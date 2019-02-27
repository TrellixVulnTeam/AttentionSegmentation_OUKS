from typing import Dict, List, Sequence, Iterable
import itertools
import logging
import logging
import re
import pdb

from overrides import overrides

from allennlp.common import Params
from allennlp.data.dataset_readers.dataset_reader \
    import DatasetReader
from allennlp.common.checks import ConfigurationError
from allennlp.data.dataset_readers \
    import Conll2003DatasetReader, DatasetReader
from allennlp.data.instance import Instance
from allennlp.data.token_indexers import TokenIndexer, SingleIdTokenIndexer
from allennlp.data.tokenizers import Token
from allennlp.data.fields \
    import MultiLabelField, TextField, SequenceLabelField

from AttentionSegmentation.reader.label_indexer import LabelIndexer
import AttentionSegmentation.commons.constants as constants


logger = logging.getLogger(__name__)
# NUM_TOKEN = "@@NUM@@"
NUM_TOKEN = "@0@"


def _is_divider(line: str) -> bool:
    empty_line = line.strip() == ''
    if empty_line:
        return True
    else:
        first_token = line.split()[0]
        if first_token == "-DOCSTART-":  # pylint: disable=simplifiable-if-statement
            return True
        else:
            return False


_VALID_LABELS = {'ner', 'pos', 'chunk'}


class WeakConll2003DatasetReader(DatasetReader):
    """
    Reader for Conll 2003 dataset. Almost identical to AllenNLP's Conll2003DatasetReader, except that it
    returns an additional MultiLabelField that represents which NER tags were present in the sentence in 
    question.

    Parameters
    ----------
    token_indexers : ``Dict[str, TokenIndexer]``, optional (default=``{"tokens": SingleIdTokenIndexer()}``)
        We use this to define the input representation for the text.  See :class:`TokenIndexer`.
    tag_label: ``str``, optional (default=``ner``)
        Specify `ner`, `pos`, or `chunk` to have that tag loaded into the instance field `tag`.
    feature_labels: ``Sequence[str]``, optional (default=``()``)
        These labels will be loaded as features into the corresponding instance fields:
        ``pos`` -> ``pos_tags``, ``chunk`` -> ``chunk_tags``, ``ner`` -> ``ner_tags``
        Each will have its own namespace: ``pos_tags``, ``chunk_tags``, ``ner_tags``.
        If you want to use one of the tags as a `feature` in your model, it should be
        specified here.
    coding_scheme: ``str``, optional (default=``IOB1``)
        Specifies the coding scheme for ``ner_labels`` and ``chunk_labels``.
        Valid options are ``IOB1`` and ``BIOUL``.  The ``IOB1`` default maintains
        the original IOB1 scheme in the CoNLL 2003 NER data.
        In the IOB1 scheme, I is a token inside a span, O is a token outside
        a span and B is the beginning of span immediately following another
        span of the same type.
    label_namespace: ``str``, optional (default=``labels``)
        Specifies the namespace for the chosen ``tag_label``.
    """

    def __init__(self,
                 token_indexers: Dict[str, TokenIndexer] = None,
                 tag_label: str = "ner",
                 feature_labels: Sequence[str] = (),
                 lazy: bool = False,
                 convert_numbers: bool = False,
                 coding_scheme: str = "IOB1",
                 label_indexer: LabelIndexer = None,
                 max_sentence_length: int = -1,
                 mask_set: str = None) -> None:
        super(WeakConll2003DatasetReader, self).__init__(lazy)
        self._token_indexers = token_indexers or {
            'tokens': SingleIdTokenIndexer()}
        if tag_label is not None and tag_label not in _VALID_LABELS:
            raise ConfigurationError("unknown tag label type: {}".format(
                tag_label))
        for label in feature_labels:
            if label not in _VALID_LABELS:
                raise ConfigurationError(
                    "unknown feature label type: {}".format(label))
        if coding_scheme not in ("IOB1", "BIOUL"):
            raise ConfigurationError(
                "unknown coding_scheme: {}".format(coding_scheme))

        self.tag_label = tag_label
        self.feature_labels = set(feature_labels)
        self.coding_scheme = coding_scheme
        self.max_sentence_length = max_sentence_length

        self.label_indexer = label_indexer
        self.convert_numbers = convert_numbers
        self._mask_set = set()
        self._mask_token_indexer = {
            "tokens": SingleIdTokenIndexer()
        }
        if mask_set is not None:
            self._mask_set = getattr(constants, mask_set)

    def get_label_indexer(self):
        return self.label_indexer

    @overrides
    def _read(self, file_path: str) -> Iterable[Instance]:
        # if `file_path` is a URL, redirect to the cache
        instances = []
        with open(file_path, "r") as data_file:
            logger.info(
                "Reading instances from lines in file at: %s", file_path)

            # Group into alternative divider / sentence chunks.
            for is_divider, lines in itertools.groupby(data_file, _is_divider):
                # Ignore the divider chunks, so that `lines`
                # corresponds to the words of a single sentence.
                if not is_divider:
                    fields = [line.strip().split() for line in lines]
                    # unzipping trick returns tuples, but our Fields need lists
                    tokens, pos_tags, chunk_tags, ner_tags = \
                        None, None, None, None
                    unzipped_fields = [
                        list(field) for field in zip(*fields)
                    ]
                    # pdb.set_trace()
                    if self.max_sentence_length > 0 and \
                        self.max_sentence_length < len(unzipped_fields[0]):
                        logger.warning(
                            f"Found sentence length: {len(unzipped_fields[0])} "
                            f"in file {file_path}, which is greater that set "
                            f"max sentence length of {self.max_sentence_length}."
                            f" Splitting"
                        )
                    if len(unzipped_fields) == 4:
                        # English provides all 4
                        tokens, pos_tags, chunk_tags, ner_tags = \
                            unzipped_fields
                    elif len(unzipped_fields) == 3:
                        # The other languages don't provide coarse chunking
                        tokens, pos_tags, ner_tags = unzipped_fields
                    else:
                        raise RuntimeError(
                            f"Found length {len(unzipped_fields)}"
                        )
                    # TextField requires ``Token`` objects
                    new_tokens = []
                    mask_tokens = []
                    for token in tokens:
                        if self.convert_numbers:
                            token = re.sub(r"[0-9]+", NUM_TOKEN, token)
                        new_tokens.append(Token(token))
                        mask_tok = Token(text=token)
                        mask_tok.text_id = 1 \
                            if mask_tok.text.lower() in self._mask_set \
                            else 0
                        mask_tokens.append(mask_tok)
                    stepsize = self.max_sentence_length if \
                        self.max_sentence_length > 0 else \
                        len(unzipped_fields[0])
                    for ix in range(0, len(new_tokens), stepsize):
                        sequence = TextField(
                            new_tokens[ix: ix + stepsize],
                            self._token_indexers
                        )
                        attn_mask_seq = TextField(
                            mask_tokens[ix: ix + stepsize],
                            self._mask_token_indexer
                        )
                        instance_fields: Dict[str, Field] = {
                            'tokens': sequence,
                            'attn_mask': attn_mask_seq
                        }
                        # Recode the labels if necessary.
                        coded_chunks = None
                        if self.coding_scheme == "BIOUL":
                            if chunk_tags is not None:
                                coded_chunks = iob1_to_bioul(
                                    chunk_tags[ix: ix + stepsize])
                            coded_ner = iob1_to_bioul(
                                ner_tags[ix: ix + stepsize])
                        else:
                            # the default IOB1
                            if chunk_tags is not None:
                                coded_chunks = chunk_tags[ix: ix + stepsize]
                            coded_ner = ner_tags[ix: ix + stepsize]

                        # Add "feature labels" to instance
                        if 'pos' in self.feature_labels:
                            instance_fields['pos_tags'] = SequenceLabelField(
                                pos_tags[ix: ix + stepsize],
                                sequence, "pos_tags")
                        if 'chunk' in self.feature_labels:
                            instance_fields['chunk_tags'] = SequenceLabelField(
                                coded_chunks,
                                sequence, "chunk_tags")
                        if 'ner' in self.feature_labels:
                            instance_fields['ner_tags'] = SequenceLabelField(
                                coded_ner,
                                sequence, "ner_tags")

                        # Add "tag label" to instance
                        if self.tag_label == 'ner':
                            try:
                                instance_fields['tags'] = SequenceLabelField(
                                    coded_ner, sequence)
                            except Exception as e:
                                import pdb; pdb.set_trace()
                        elif self.tag_label == 'pos':
                            instance_fields['tags'] = SequenceLabelField(
                                pos_tags, sequence)
                        elif self.tag_label == 'chunk':
                            instance_fields['tags'] = SequenceLabelField(
                                coded_chunks, sequence)
                        if self.label_indexer is not None:
                            instance_fields["labels"] = \
                                self.label_indexer.index(
                                coded_ner,
                                as_label_field=True
                            )
                        instances.append(Instance(instance_fields))
        
        for ix, instance in enumerate(instances):
            assert len(instance.fields["tokens"].tokens) == len(instance.fields["tags"].labels)
            if self.max_sentence_length > 0:
                assert len(instance.fields["tokens"].tokens) <= self.max_sentence_length
            if instance.fields["tags"].labels[0].startswith("I-"):
                logger.info(f"Made correction in {file_path}")
                instance.fields["tags"].labels[0] = \
                    re.sub("I-", "B-", instance.fields["tags"].labels[0])
        return instances

    @classmethod
    @overrides
    def from_params(cls, params: Params) -> 'WeakConll2003DatasetReader':
        token_indexers = TokenIndexer.dict_from_params(
            params.pop('token_indexers', {}))
        tag_label = params.pop('tag_label', None)
        feature_labels = params.pop('feature_labels', ())
        lazy = params.pop('lazy', False)
        coding_scheme = params.pop('coding_scheme', 'IOB1')
        label_indexer_params = params.pop('label_indexer', None)
        label_indexer = None
        if label_indexer_params is not None:
            label_indexer = LabelIndexer.from_params(label_indexer_params)
        convert_numbers = params.pop("convert_numbers", False)
        max_sentence_length = params.pop("max_sentence_length", -1)
        mask_set = params.pop("mask_set", None)
        params.assert_empty(cls.__name__)
        return WeakConll2003DatasetReader(token_indexers=token_indexers,
                                          tag_label=tag_label,
                                          feature_labels=feature_labels,
                                          lazy=lazy,
                                          convert_numbers=convert_numbers,
                                          coding_scheme=coding_scheme,
                                          label_indexer=label_indexer,
                                          max_sentence_length=max_sentence_length,
                                          mask_set=mask_set)
