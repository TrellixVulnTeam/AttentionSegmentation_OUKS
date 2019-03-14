from allennlp.models.model import Model
from allennlp.common import Params, Registrable
from allennlp.data import Vocabulary
from AttentionSegmentation.reader.label_indexer import LabelIndexer


class BaseClassifier(Model, Registrable):
    def __init__(self, vocab, regularizer, *args, **kwargs):
        super(BaseClassifier, self).__init__(
            vocab=vocab,
            regularizer=regularizer
        )

    @classmethod
    def from_params(
        cls,
        params: Params,
        vocab: Vocabulary,
        label_indexer: LabelIndexer
    ):
        model_type = params.pop("type")
        return cls.by_name(model_type).from_params(
            params=params,
            vocab=vocab,
            label_indexer=label_indexer
        )
