import torch
from transformers import PreTrainedModel, BertConfig, BertModel, BertLMHeadModel, BertTokenizer
from transformers.trainer import Trainer
from torch.utils.data import DataLoader
from typing import Optional
import os

import pytorch_lightning as pl
from tools import get_stop_ids


class TILDE(pl.LightningModule):
    def __init__(self, model_type, from_pretrained=None, gradient_checkpointing=False):
        super().__init__()
        if from_pretrained is not None:
            self.bert = BertLMHeadModel.from_pretrained(from_pretrained, cache_dir="./cache")
        else:
            self.config = BertConfig.from_pretrained(model_type, cache_dir="./cache")
            self.config.gradient_checkpointing = gradient_checkpointing  # for trade off training speed for larger batch size
            print(self.config.gradient_checkpointing)
            # self.config.is_decoder = True
            self.bert = BertLMHeadModel.from_pretrained(model_type, config=self.config, cache_dir="./cache")

        self.tokenizer = BertTokenizer.from_pretrained(model_type, cache_dir="./cache")
        self.num_valid_tok = self.tokenizer.vocab_size - len(get_stop_ids(self.tokenizer))

    def forward(self, x):
        input_ids, token_type_ids, attention_mask = x
        outputs = self.bert(input_ids=input_ids,
                            token_type_ids=token_type_ids,
                            attention_mask=attention_mask,
                            return_dict=True).logits[:, 0]
        return outputs

    # Bi-direction loss (BDQLM)
    def training_step(self, batch, batch_idx):
        # todo test all_gather: self.all_gather()
        passage_input_ids, passage_token_type_ids, passage_attention_mask, yqs, neg_yqs, \
        query_input_ids, query_token_type_ids, query_ttention_mask, yds, neg_yds = batch

        passage_outputs = self.bert(input_ids=passage_input_ids,
                                    token_type_ids=passage_token_type_ids,
                                    attention_mask=passage_attention_mask,
                                    return_dict=True).logits[:, 0]

        query_outputs = self.bert(input_ids=query_input_ids,
                                  token_type_ids=query_token_type_ids,
                                  attention_mask=query_ttention_mask,
                                  return_dict=True).logits[:, 0]

        batch_size = len(yqs)
        batch_passage_prob = torch.sigmoid(passage_outputs)
        batch_query_prob = torch.sigmoid(query_outputs)

        passage_pos_loss = 0
        query_pos_loss = 0
        for i in range(batch_size):

            # BCEWithLogitsLoss
            passage_pos_ids_plus = torch.where(yqs[i] == 1)[0]
            passage_pos_ids_minus = torch.where(yqs[i] == 0)[0]

            passage_pos_probs = batch_passage_prob[i][passage_pos_ids_plus]
            passage_neg_probs = batch_passage_prob[i][passage_pos_ids_minus]
            passage_pos_loss -= torch.sum(torch.log(passage_pos_probs)) + torch.sum(torch.log(1-passage_neg_probs))

            query_pos_ids_plus = torch.where(yds[i] == 1)[0]
            query_pos_ids_minus = torch.where(yds[i] == 0)[0]

            query_pos_probs = batch_query_prob[i][query_pos_ids_plus]
            query_neg_probs = batch_query_prob[i][query_pos_ids_minus]
            query_pos_loss -= torch.sum(torch.log(query_pos_probs)) + torch.sum(torch.log(1-query_neg_probs))

        loss = (passage_pos_loss + query_pos_loss) / (self.num_valid_tok * 2)

        self.log("loss", loss)
        return loss

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=2e-5)
        return optimizer

    def save(self, path):
        self.bert.save_pretrained(path)


class TILDEv2(PreTrainedModel):
    config_class = BertConfig
    base_model_prefix = "tildev2"

    def __init__(self, config: BertConfig, train_group_size=8):
        super().__init__(config)
        self.config = config
        self.bert = BertModel(config)
        self.tok_proj = torch.nn.Linear(config.hidden_size, 1)
        self.cross_entropy = torch.nn.CrossEntropyLoss(reduction='mean')
        self.train_group_size = train_group_size
        self.init_weights()

    # Copied from transformers.models.bert.modeling_bert.BertPreTrainedModel._init_weights
    def _init_weights(self, module):
        """ Initialize the weights """
        if isinstance(module, (torch.nn.Linear, torch.nn.Embedding)):
            # Slightly different from the TF version which uses truncated_normal for initialization
            # cf https://github.com/pytorch/pytorch/pull/5617
            module.weight.data.normal_(mean=0.0, std=self.config.initializer_range)
        elif isinstance(module, torch.nn.LayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)
        if isinstance(module, torch.nn.Linear) and module.bias is not None:
            module.bias.data.zero_()

    def init_weights(self):
        self.bert.init_weights()
        self.tok_proj.apply(self._init_weights)

    def encode(self, **features):
        assert all([x in features for x in ['input_ids', 'attention_mask', 'token_type_ids']])
        model_out = self.bert(**features, return_dict=True)
        reps = self.tok_proj(model_out.last_hidden_state)
        tok_weights = torch.relu(reps)
        return tok_weights

    def forward(self, qry_in, doc_in):
        qry_input = qry_in
        doc_input = doc_in
        doc_out = self.bert(**doc_input, return_dict=True)
        doc_reps = self.tok_proj(doc_out.last_hidden_state)  # D * LD * d


        doc_reps = torch.relu(doc_reps) # relu to make sure no negative weights
        doc_input_ids = doc_input['input_ids']

        # mask ingredients
        qry_input_ids = qry_input['input_ids']
        qry_attention_mask = qry_input['attention_mask']
        self.mask_sep(qry_attention_mask)

        qry_reps = torch.ones_like(qry_input_ids, dtype=torch.float32, device=doc_reps.device).unsqueeze(2)
        tok_scores = self.compute_tok_score_cart(
            doc_reps, doc_input_ids,
            qry_reps, qry_input_ids, qry_attention_mask
        )  # Q * D

        scores = tok_scores

        labels = torch.arange(
            scores.size(0),
            device=doc_input['input_ids'].device,
            dtype=torch.long
        )

        # offset the labels
        labels = labels * self.train_group_size
        loss = self.cross_entropy(scores, labels)
        return loss, scores.view(-1)

    def mask_sep(self, qry_attention_mask):
        sep_pos = qry_attention_mask.sum(1).unsqueeze(1) - 1  # the sep token position
        _zeros = torch.zeros_like(sep_pos)
        qry_attention_mask.scatter_(1, sep_pos.long(), _zeros)
        return qry_attention_mask

    def compute_tok_score_cart(self, doc_reps, doc_input_ids, qry_reps, qry_input_ids, qry_attention_mask):
        qry_input_ids = qry_input_ids.unsqueeze(2).unsqueeze(3)  # Q * LQ * 1 * 1
        doc_input_ids = doc_input_ids.unsqueeze(0).unsqueeze(1)  # 1 * 1 * D * LD
        exact_match = doc_input_ids == qry_input_ids  # Q * LQ * D * LD
        exact_match = exact_match.float()
        scores_no_masking = torch.matmul(
            qry_reps.view(-1, 1),  # (Q * LQ) * d
            doc_reps.view(-1, 1).transpose(0, 1)  # d * (D * LD)
        )
        scores_no_masking = scores_no_masking.view(
            *qry_reps.shape[:2], *doc_reps.shape[:2])  # Q * LQ * D * LD
        # scores_no_masking = scores_no_masking.permute(0, 2, 1, 3)  # Q * D * LQ * LD

        scores, _ = (scores_no_masking * exact_match).max(dim=3)  # Q * LQ * D, max pooling
        tok_scores = (scores * qry_attention_mask.unsqueeze(2))[:, 1:].sum(1)

        return tok_scores


class TILDEv2Trainer(Trainer):
    def _save(self, output_dir: Optional[str] = None):
        output_dir = output_dir if output_dir is not None else self.args.output_dir
        os.makedirs(output_dir, exist_ok=True)

        self.model.save_pretrained(output_dir)
        if self.tokenizer is not None and self.is_world_process_zero():
            self.tokenizer.save_pretrained(output_dir)
        torch.save(self.args, os.path.join(output_dir, "training_args.bin"))

    def _prepare_inputs(self, inputs):
        prepared = {}
        for k, v in inputs.items():
            prepared[k] = {}
            for sk, sv in v.items():
                if isinstance(sv, torch.Tensor):
                    prepared[k][sk] = sv.to(self.args.device)

        return prepared

    def create_optimizer_and_scheduler(self, num_training_steps: int):
        if self.args.warmup_ratio > 0:
            self.args.warmup_steps = num_training_steps * self.args.warmup_ratio

        super().create_optimizer_and_scheduler(num_training_steps)

    def get_train_dataloader(self) -> DataLoader:
        if self.train_dataset is None:
            raise ValueError("Trainer: training requires a train_dataset.")
        train_sampler = self._get_train_sampler()

        return DataLoader(
            self.train_dataset,
            batch_size=self.args.train_batch_size,
            sampler=train_sampler,
            collate_fn=self.data_collator,
            drop_last=True,
            num_workers=self.args.dataloader_num_workers,
        )