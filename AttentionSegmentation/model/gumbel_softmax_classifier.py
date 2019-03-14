from __future__ import absolute_import
from overrides import overrides
from collections import OrderedDict
from typing import Optional, Dict, List
import numpy as np
import torch
from copy import deepcopy
from torch.nn import Linear
import torch.nn.functional as F
from torch import Tensor, LongTensor
import logging
import pdb

# import AttentionSegmentation.model as Attns
import allennlp.nn.util as util
from allennlp.nn import \
    InitializerApplicator, RegularizerApplicator
from allennlp.modules import \
    Seq2SeqEncoder, TextFieldEmbedder
from allennlp.data import Vocabulary
from allennlp.common.checks import check_dimensions_match
from allennlp.models.model import Model
from allennlp.common.params import Params, Registrable
from allennlp.training.metrics import \
    BooleanAccuracy

from AttentionSegmentation.reader.label_indexer import LabelIndexer
from AttentionSegmentation.commons.utils import to_numpy
import AttentionSegmentation.model as Attns
from AttentionSegmentation.model.metrics import ClassificationMetrics

logger = logging.getLogger(__name__)

class Generator(torch.nn.Module, Registrable):
    @classmethod
    def from_params(cls, params: Params):
        gen_type = params.pop("type")
        return cls.by_name(gen_type).from_params(params)

@Generator.register("basic_generator")
class BasicGenerator(Generator):
    @overrides
    def forward(
        self,
        emb_msg: torch.Tensor,
        mask: torch.LongTensor
    ): -> torch.Tensor:
        logits = self.encoder_word(emb_msg, mask)
        attentions = self.prob_layer(logits)
        return attentions


class BaseClassifier(Model):
    """This class is similar to the previous one, except that
    it handles multi level classification
    """

    def __init__(
        self,
        vocab: Vocabulary,
        text_field_embedder: TextFieldEmbedder,
        generator: Generator,
        sampler: Sampler,
        identifier: Identifier,
        label_indexer: LabelIndexer,
        thresh: float = 0.5,
        initializer: InitializerApplicator = InitializerApplicator(),
        regularizer: Optional[RegularizerApplicator] = None
    ) -> 'MultiClassifier':
        super(BaseClassifier, self).__init__(vocab, regularizer)
        # Label info
        self.label_indexer = label_indexer
        self.num_labels = self.label_indexer.get_num_tags()

        # Prediction thresholds
        self.thresh = thresh
        self.log_thresh = np.log(thresh + 1e-5)
        # Model
        # Text encoders
        self.text_field_embedder = text_field_embedder
        self.generator = generator

        # Attention Modules
        # We use setattr, so that cuda properties translate.
        self.identifier = identifier

        self.classification_metric = ClassificationMetrics(
            label_indexer)
        # self.classification_metric = BooleanAccuracy()
        initializer(self)

        # Some dimension checks
        # FIXME:
        # Do Dimension Checks
        # check_dimensions_match(
        #     text_field_embedder.get_output_dim(), encoder_word.get_input_dim(),
        #     "text field embedding dim", "word encoder input dim")
        # check_dimensions_match(
        #     encoder_word.get_output_dim(), attn_word[0].get_input_dim(),
        #     "word encoder output", "word attention input")

    @overrides
    def forward(
        self,
        tokens: Dict[str, LongTensor],
        labels: LongTensor = None,
        tags: LongTensor = None,
        **kwargs
    ) -> Dict[str, Tensor]:
        """The forward pass

        Commonly used symbols:
            S : Max sent length
            C : Max word length
            L : Number of tags (Including the O tag)

        Arguments:
            tokens (Dict[str, ``LongTensor``]): The indexed values
                Contains the following:
                    * tokens: batch_size x S
                    * chars: batch_size x S x C
                    * elmo [Optional]: batch_size x S x C

            labels (``LongTensor``) : batch x L: The labels
            tags (``LongTensor``) : batch x S : The gold NER tags

        ..note::
            Need to incorporate pos_tags etc. into kwargs

        Returns:
            Dict[str, ``LongTensor``]: A dictionary with the following
            attributes

                * loss: 1 x 1 : The BCE Loss
                * logits: (batch, num_tags) : The output of the logits
                    for class prediction
                * log_probs: (batch, num_tags) : The output
                    for class prediction
                * attentions: List[batch x S]:
                  The attention over each word in the sentence,
                  for each tag
                * preds: (batch, num_tags) : The probabilites predicted
        """
        if len(kwargs) > 0:
            raise NotImplementedError("Don't handle features yet")
        emb_msg = self.text_field_embedder(tokens)
        mask = util.get_text_field_mask(tokens)
        attentions = self.generator(emb_msg, mask)
        samples = None
        if self.sampler is not None:
            samples = self.sampler(attentions, mask)
        else:
            samples = attentions
        outputs = self.identifier(emb_msg, mask, samples, attentions, labels)
        return outputs

    @overrides
    def get_metrics(self, reset: bool = False) -> Dict[str, float]:
        metric_dict = self.classification_metric.get_metric(
            reset=reset)
        # return OrderedDict({x: y for x, y in metric_dict.items()})
        return metric_dict

    @overrides
    def decode(self, outputs):
        """
        This decodes the outputs of the model into a format used downstream
        for predictions

        Arguments:
            outputs (List[Dict]) : The outputs generated by the model
                Must contain
                    * mask : The mask for the current batch
                    * preds (batch x num_tags - 1) : The predictions
                        note that if nothing is predicted, we predict "O"
                    * attentions (batch x seq_len x num_tags) : The attentions

        Returns:
            decoded_output (Dict) : The decoded output
                Must contain:
                    * preds (List[List[str]]) : The predicted tags
                    * attentions (List[Dict]) : List of dictionaries
                        mapping each tag to its attention distribution
        """
        decoded_output = {
            "preds": [],
            "attentions": []
        }
        lengths = outputs["mask"].sum(-1)
        lengths = to_numpy(lengths, lengths.is_cuda)
        predictions = to_numpy(outputs["preds"], outputs["preds"].is_cuda)
        log_probs = to_numpy(
            outputs["log_probs"], outputs["log_probs"].is_cuda)
        attentions = to_numpy(
            outputs["attentions"], outputs["attentions"].is_cuda)
        for ix in range(lengths.size):
            non_zero_indices = np.nonzero(predictions[ix])[0]
            pred_list = []
            for kx in range(non_zero_indices.shape[0]):
                pred_list.append(
                    [
                        self.label_indexer.get_tag(
                            non_zero_indices[kx]
                        ),
                        np.exp(log_probs[ix, non_zero_indices[kx]])
                    ]
                )

            if len(pred_list) == 0:
                pred_list.append(["O", 1.0])
            decoded_output["preds"].append(pred_list)
            attention = OrderedDict()
            for jx in range(attentions[ix].shape[-1]):
                tag = self.label_indexer.get_tag(jx)
                attention[tag] = attentions[ix, :lengths[ix], jx].tolist()
            decoded_output["attentions"].append(attention)
        return decoded_output

    @classmethod
    @overrides
    def from_params(
        cls,
        vocab: Vocabulary,
        params: Params,
        label_indexer: LabelIndexer
    ) -> 'BaseClassifier':
        
        num_tags = label_indexer.get_num_tags()
        embedder_params = params.pop("text_field_embedder")
        text_field_embedder = TextFieldEmbedder.from_params(
            vocab, embedder_params
        )
        gen_params = params.pop("generator_params")
        generator = Generator.from_params(
            gen_params
        )

        sampler_params = params.pop("sampler_params")
        sampler = Sampler.from_params(
            sampler_params
        )

        identifier_params = params.pop("identifier_params")
        identifier = Identifier.from_params(
            identifier_params
        )

        threshold = params.pop("threshold", 0.5)
        initializer = InitializerApplicator.from_params(
            params.pop('initializer', [])
        )
        regularizer = RegularizerApplicator.from_params(
            params.pop('regularizer', [])
        )
        return cls(
            vocab=vocab,
            text_field_embedder=text_field_embedder,
            generator=generator,
            sampler=sampler,
            identifier=identifier,
            thresh=threshold,
            initializer=initializer,
            regularizer=regularizer,
            label_indexer=label_indexer
        )