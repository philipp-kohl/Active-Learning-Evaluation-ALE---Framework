from typing import List, Dict

import torch
import torchmetrics
from lightning import LightningModule
from torch import optim, softmax
from torchmetrics import Metric
from transformers import AutoModel

from ale.registry.registerable_model import ModelRegistry
from ale.trainer.lightning.modules.flair_crf import CRF, ViterbiLoss, ViterbiDecoder, START_TAG, STOP_TAG
from ale.trainer.lightning.utils import derive_labels, create_metrics, LabelGeneralizer, is_valid_for_prog_bar


@ModelRegistry.register("trf_crf")
class TransformerCrfLightning(LightningModule):
    def __init__(self, model_name: str, labels: List[str], learn_rate: float, weight_decay: float,
                 ignore_labels: List[str] = None, **kwargs):
        super().__init__()
        self.save_hyperparameters()
        if ignore_labels is None:
            ignore_labels = []

        self.id2label, self.label2id, self.bio_id_to_coarse_label_id = derive_labels(labels)
        self.label2id[START_TAG] = len(self.label2id)
        self.label2id[STOP_TAG] = len(self.label2id)
        self.id2label[self.label2id[START_TAG]] = START_TAG
        self.id2label[self.label2id[STOP_TAG]] = STOP_TAG

        self.model = AutoModel.from_pretrained(model_name, num_labels=len(self.id2label),
                                               id2label=self.id2label, label2id=self.label2id)
        self.learn_rate = learn_rate
        self.ignore_labels = ignore_labels
        self.weight_decay = weight_decay
        self.num_labels = len(self.id2label)
        self.raw_labels = ['O'] + labels

        self.linear = torch.nn.Linear(self.model.config.hidden_size, len(self.label2id))

        self.crf = CRF(self.label2id)
        self.crf_loss = ViterbiLoss(self.label2id)
        self.crf_decoder = ViterbiDecoder(self.label2id)

        self.train_f1_per_label_wo_bio = torchmetrics.F1Score(task="multiclass", num_classes=len(labels) + 1,
                                                              average=None)
        self.val_f1_per_label_wo_bio = torchmetrics.F1Score(task="multiclass", num_classes=len(labels) + 1,
                                                            average=None)
        self.test_f1_per_label_wo_bio = torchmetrics.F1Score(task="multiclass", num_classes=len(labels) + 1,
                                                             average=None)
        self.train_metrics = create_metrics(self.num_labels)
        self.val_metrics = create_metrics(self.num_labels)
        self.test_metrics = create_metrics(self.num_labels)

    def generalize_labels(self, labels):
        label_generalizer = LabelGeneralizer(self.bio_id_to_coarse_label_id, self.device)
        return label_generalizer.generalize_labels(labels)

    def move_metrics_to_device(self, metrics_dict):
        for metric_name, metric in metrics_dict.items():
            metrics_dict[metric_name] = metric.to(self.device)

    def on_fit_start(self):
        self.model = self.model.to(self.device)
        self.move_metrics_to_device(self.train_metrics)
        self.move_metrics_to_device(self.val_metrics)
        self.move_metrics_to_device(self.test_metrics)
        self.crf_loss = self.crf_loss.to(self.device)
        self.crf_decoder = self.crf_decoder.to(self.device)

    def forward(self, input_ids, attention_mask, labels=None, **kwargs):
        embeddings = self.model(input_ids, attention_mask=attention_mask)
        features = self.linear(embeddings.last_hidden_state)
        scores = self.crf(features)

        loss = 0.0
        if labels is not None:
            loss = self.crf_loss(scores, attention_mask.sum(dim=1), self.crf.transitions, labels)

        decoded = self.crf_decoder.decode(scores, True)
        return loss, decoded

    def training_step(self, batch, batch_idx):
        loss, decoded = self(**batch)

        self.evaluate(batch, cls_per_token, self.train_metrics, self.train_f1_per_label_wo_bio)
        self.log("train_loss", loss)
        return loss

    def validation_step(self, batch, batch_idx):
        cls_per_token = self(**batch)
        loss = -self.crf(cls_per_token, mask=batch["attention_mask"], tags=batch["labels"])
        self.evaluate(batch, cls_per_token, self.val_metrics, self.val_f1_per_label_wo_bio)
        self.log_dict({'val_loss': loss})

    def test_step(self, batch, batch_idx):
        cls_per_token = self(**batch)
        loss = -self.crf(cls_per_token, mask=batch["attention_mask"], tags=batch["labels"])
        self.evaluate(batch, cls_per_token, self.test_metrics, self.test_f1_per_label_wo_bio)
        self.log_dict({'test_loss': loss})

    def predict_step(self, batch, batch_idx, dataloader_idx=0):
        mask = batch['attention_mask']

        outputs = self(**batch)
        predictions = self.crf.decode(outputs, mask=mask)

        softmax_logits = softmax(logits, dim=-1)
        class_predictions = torch.argmax(softmax_logits, dim=-1)
        class_predictions_numpy = class_predictions.cpu().numpy() if class_predictions.is_cuda else class_predictions.numpy()
        class_predictions_numpy = self.apply_mask(mask, class_predictions_numpy)

        token_labels = []
        for sequence in class_predictions_numpy:
            sequence_labels = [self.id2label[index] for index in sequence]
            token_labels.append(sequence_labels)
        token_labels = self.apply_mask(mask, token_labels)

        confidences = []
        confidences_numpy = softmax_logits.cpu().numpy() if softmax_logits.is_cuda else softmax_logits.numpy()
        for conf_batch in confidences_numpy:
            single_sequence = []
            for token in conf_batch:
                single_sequence.append(
                    {self.id2label[idx]: token_confidence for idx, token_confidence in enumerate(token)})
            confidences.append(single_sequence)
        confidences = self.apply_mask(mask, confidences)

        result = {'tokens': batch['token_text'], 'token_labels': token_labels,
                  'text': batch['text'], 'offset_mapping': batch['offset_mapping'],
                  'confidences': confidences}

        if "labels" in batch:
            gold_labels = []
            for sequence in batch["labels"].cpu().numpy():
                sequence_labels = [self.id2label[index] if index != -100 else "<PAD>" for index in sequence]
                gold_labels.append(sequence_labels)
            gold_labels = self.apply_mask(mask, gold_labels)
            result["gold_labels"] = gold_labels

        return result

    def apply_mask(self, mask, value):
        result = []
        for m, v in zip(mask, value):
            seq = []
            for m1, v1 in zip(m, v):
                if m1 == 1:
                    seq.append(v1)
            result.append(seq)

        return result

    def compute_and_log_metrics(self, prefix: str, metrics: Dict[str, Metric], f1_per_label: Metric):
        for metric_name, metric in metrics.items():
            self.log(f"{prefix}_{metric_name}", metric.compute(), prog_bar=is_valid_for_prog_bar(metric_name))
        for idx, score in enumerate(f1_per_label.compute()):
            self.log(f"{prefix}_f1_{self.raw_labels[idx]}", score)

    def on_validation_epoch_end(self):
        self.compute_and_log_metrics('val', self.val_metrics, self.train_f1_per_label_wo_bio)

    def on_train_epoch_end(self):
        self.compute_and_log_metrics('train', self.train_metrics, self.val_f1_per_label_wo_bio)

    def on_test_epoch_end(self):
        self.compute_and_log_metrics('test', self.test_metrics, self.test_f1_per_label_wo_bio)

    def configure_optimizers(self):
        optimizer = optim.AdamW(self.parameters(), lr=self.learn_rate, weight_decay=self.weight_decay)
        return optimizer

    def evaluate(self, batch, cls_per_token, metrics: Dict[str, Metric], f1_per_label: Metric):
        mask = batch["attention_mask"]
        decoded_batch = self.crf.decode(cls_per_token, mask=batch["attention_mask"])
        # Determine the maximum sequence length
        max_len = max(len(seq) for seq in decoded_batch)

        # Create a padded tensor for the predicted tags
        padded_preds = torch.full((len(decoded_batch), max_len), fill_value=-100,
                                  dtype=torch.int64,
                                  device=self.device)  # Use a fill value that is ignored in your metric calculation
        for i, seq in enumerate(decoded_batch):
            padded_preds[i, :len(seq)] = torch.tensor(seq, dtype=torch.int64)

        gold_labels = batch["labels"]

        mask_flat = mask.view(-1)
        gold_labels_flat = gold_labels.view(-1)
        prediction_labels_flat = padded_preds.view(-1)
        # Apply mask: Set predictions to -1 where mask is 0 (padded)
        prediction_labels_flat = torch.where(mask_flat == 1, prediction_labels_flat,
                                             torch.tensor(-1, device=self.device))
        gold_labels_flat = torch.where(mask_flat == 1, gold_labels_flat, torch.tensor(-1, device=self.device))
        prediction_labels_flat_with_ignore = prediction_labels_flat
        gold_labels_flat_with_ignore = gold_labels_flat
        for l in self.ignore_labels:
            label_idx = self.label2id[l]
            prediction_labels_flat_with_ignore = torch.where(prediction_labels_flat != label_idx,
                                                             prediction_labels_flat,
                                                             torch.tensor(-1, device=self.device))
            gold_labels_flat_with_ignore = torch.where(gold_labels_flat != label_idx, gold_labels_flat,
                                                       torch.tensor(-1, device=self.device))
        # Filter out the ignored indices (-1) before passing them to the metrics
        valid_indices = gold_labels_flat_with_ignore != -1  # Assuming -1 is used to mark padded or ignored labels
        valid_gold_labels = gold_labels_flat_with_ignore[valid_indices]
        valid_prediction_labels = prediction_labels_flat_with_ignore[valid_indices]
        # Update metrics with filtered valid labels and predictions

        t1 = self.generalize_labels(prediction_labels_flat)
        t2 = self.generalize_labels(gold_labels_flat)
        for metric_name, metric in metrics.items():
            if len(valid_prediction_labels) > 0:  # TODO why are labels all -1?
                metric.update(valid_prediction_labels, valid_gold_labels)
        f1_per_label.update(t1, t2)
