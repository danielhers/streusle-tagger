from typing import Any, Dict, List, Optional, Set, Tuple
import json
import logging

from overrides import overrides
import torch
from torch.nn.modules.linear import Linear

from allennlp.common.checks import check_dimensions_match
from allennlp.data import Vocabulary
from allennlp.modules import Seq2SeqEncoder, TimeDistributed, TextFieldEmbedder
from allennlp.modules import ConditionalRandomField, FeedForward
from allennlp.models.model import Model
from allennlp.nn import InitializerApplicator, RegularizerApplicator
import allennlp.nn.util as util
from allennlp.training.metrics import CategoricalAccuracy

ALL_UPOS = {'X', 'INTJ', 'VERB', 'ADV', 'CCONJ', 'PUNCT', 'ADP',
            'NOUN', 'SYM', 'ADJ', 'PROPN', 'DET', 'PART', 'PRON', 'SCONJ', 'NUM', 'AUX'}
ALL_LEXCATS = {'N', 'INTJ', 'INF.P', 'V', 'AUX', 'PP', 'PUNCT',
               'POSS', 'X', 'PRON.POSS', 'SYM', 'PRON', 'SCONJ',
               'NUM', 'DISC', 'ADV', 'CCONJ', 'P', 'ADJ', 'DET', 'INF'}
logger = logging.getLogger(__name__)  # pylint: disable=invalid-name

@Model.register("streusle_tagger")
class StreusleTagger(Model):
    """
    The ``StreusleTagger`` embeds a sequence of text before (optionally)
    encoding it with a ``Seq2SeqEncoder`` and passing it through a ``FeedForward``
    before using a Conditional Random Field model to predict a STREUSLE lextag for
    each token in the sequence. Decoding is constrained with the "BbIiOo_~"
    tagging scheme.

    Parameters
    ----------
    vocab : ``Vocabulary``, required
        A Vocabulary, required in order to compute sizes for input/output projections.
    text_field_embedder : ``TextFieldEmbedder``, required
        Used to embed the tokens ``TextField`` we get as input to the model.
    encoder : ``Seq2SeqEncoder``, optional (default=``None``)
        The encoder that we will use in between embedding tokens and predicting output tags.
    label_namespace : ``str``, optional (default=``labels``)
        This is needed to constrain the CRF decoding.
        Unless you did something unusual, the default value should be what you want.
    feedforward : ``FeedForward``, optional, (default = None).
        An optional feedforward layer to apply after the encoder.
    include_start_end_transitions : ``bool``, optional (default=``True``)
        Whether to include start and end transition parameters in the CRF.
    dropout:  ``float``, optional (default=``None``)
    use_upos_constraints : ``bool``, optional (default=``True``)
        Whether to use UPOS constraints. If True, model shoudl recieve UPOS as input.
    initializer : ``InitializerApplicator``, optional (default=``InitializerApplicator()``)
        Used to initialize the model parameters.
    regularizer : ``RegularizerApplicator``, optional (default=``None``)
        If provided, will be used to calculate the regularization penalty during training.

    """

    def __init__(self, vocab: Vocabulary,
                 text_field_embedder: TextFieldEmbedder,
                 encoder: Seq2SeqEncoder = None,
                 label_namespace: str = "labels",
                 feedforward: Optional[FeedForward] = None,
                 include_start_end_transitions: bool = True,
                 dropout: Optional[float] = None,
                 use_upos_constraints: bool = True,
                 initializer: InitializerApplicator = InitializerApplicator(),
                 regularizer: Optional[RegularizerApplicator] = None) -> None:
        super().__init__(vocab, regularizer)

        self.label_namespace = label_namespace
        self.text_field_embedder = text_field_embedder
        self.num_tags = self.vocab.get_vocab_size(label_namespace)
        self.encoder = encoder
        if self.encoder is not None:
            encoder_output_dim = self.encoder.get_output_dim()
        else:
            encoder_output_dim = self.text_field_embedder.get_output_dim()
        if dropout:
            self.dropout = torch.nn.Dropout(dropout)
        else:
            self.dropout = None
        self.feedforward = feedforward

        if feedforward is not None:
            output_dim = feedforward.get_output_dim()
        else:
            output_dim = encoder_output_dim
        self.tag_projection_layer = TimeDistributed(Linear(output_dim,
                                                           self.num_tags))
        self._label_namespace = label_namespace
        labels = self.vocab.get_index_to_token_vocabulary(self._label_namespace)
        constraints = streusle_allowed_transitions(labels)

        self.use_upos_constraints = use_upos_constraints

        if self.use_upos_constraints:
            # Get a dict with a mapping from UPOS to allowed LEXCAT here.
            self._upos_to_allowed_lexcats: Dict[str, Set[str]] = get_upos_allowed_lexcats()
            # Use labels and the upos_to_allowed_lexcats to get a dict with
            # a mapping from UPOS to a mask with 1 at allowed label indices and 0 at
            # disallowed label indices.
            self._upos_to_label_mask: Dict[str, torch.Tensor] = {}
            for upos in ALL_UPOS:
                # Shape: (num_labels,)
                upos_label_mask = torch.zeros(len(labels),
                                              device=next(self.tag_projection_layer.parameters()).device)
                # Go through the labels and indices and fill in the values that are allowed.
                for label_index, label in labels.items():
                    if len(label.split("-")) == 1:
                        upos_label_mask[label_index] = 1
                        continue
                    label_lexcat = label.split("-")[1]
                    if not label.startswith("O-") and not label.startswith("o-"):
                        # Label does not start with O-/o-, always allowed.
                        upos_label_mask[label_index] = 1
                    elif label_lexcat in self._upos_to_allowed_lexcats[upos]:
                        # Label starts with O-/o-, but the lexcat is in allowed
                        # lexcats for the current upos.
                        upos_label_mask[label_index] = 1
                self._upos_to_label_mask[upos] = upos_label_mask

        self.include_start_end_transitions = include_start_end_transitions
        self.crf = ConditionalRandomField(
                self.num_tags, constraints,
                include_start_end_transitions=include_start_end_transitions)

        self.metrics = {
                "accuracy": CategoricalAccuracy(),
                "accuracy3": CategoricalAccuracy(top_k=3)
        }
        if encoder is not None:
            check_dimensions_match(text_field_embedder.get_output_dim(), encoder.get_input_dim(),
                                   "text field embedding dim", "encoder input dim")
        if feedforward is not None:
            check_dimensions_match(encoder.get_output_dim(), feedforward.get_input_dim(),
                                   "encoder output dim", "feedforward input dim")
        initializer(self)

    @overrides
    def forward(self,  # type: ignore
                tokens: Dict[str, torch.LongTensor],
                tags: torch.LongTensor = None,
                metadata: List[Dict[str, Any]] = None) -> Dict[str, torch.Tensor]:
        # pylint: disable=arguments-differ
        """
        Parameters
        ----------
        tokens : ``Dict[str, torch.LongTensor]``, required
            The output of ``TextField.as_array()``, which should typically be passed directly to a
            ``TextFieldEmbedder``. This output is a dictionary mapping keys to ``TokenIndexer``
            tensors.  At its most basic, using a ``SingleIdTokenIndexer`` this is: ``{"tokens":
            Tensor(batch_size, num_tokens)}``. This dictionary will have the same keys as were used
            for the ``TokenIndexers`` when you created the ``TextField`` representing your
            sequence.  The dictionary is designed to be passed directly to a ``TextFieldEmbedder``,
            which knows how to combine different word representations into a single vector per
            token in your input.
        tags : ``torch.LongTensor``, optional (default = ``None``)
            A torch tensor representing the sequence of integer gold lextags of shape
            ``(batch_size, num_tokens)``.
        metadata : ``List[Dict[str, Any]]``, optional, (default = None)
            Additional information about the example.

        Returns
        -------
        An output dictionary consisting of:

        constrained_logits : ``torch.FloatTensor``
            The constrained logits that are the output of the ``tag_projection_layer``
        mask : ``torch.LongTensor``
            The text field mask for the input tokens
        tags : ``List[List[int]]``
            The predicted tags using the Viterbi algorithm.
        loss : ``torch.FloatTensor``, optional
            A scalar loss to be optimised. Only computed if gold label ``tags`` are provided.
        """
        embedded_text_input = self.text_field_embedder(tokens)
        mask = util.get_text_field_mask(tokens)

        if self.dropout:
            embedded_text_input = self.dropout(embedded_text_input)

        if self.encoder:
            encoded_text = self.encoder(embedded_text_input, mask)
        else:
            encoded_text = embedded_text_input

        if self.dropout:
            encoded_text = self.dropout(encoded_text)

        if self.feedforward is not None:
            encoded_text = self.feedforward(encoded_text)

        logits = self.tag_projection_layer(encoded_text)

        if self.use_upos_constraints and self.use_lemma_constraints:
            # List of length (batch_size,), where each inner list is a list of
            # the UPOS tags for the associated token sequence.
            batch_upos_tags = [instance_metadata["upos_tags"] for instance_metadata in metadata]

            # List of length (batch_size,), where each inner list is a list of
            # the lemmas for the associated token sequence.
            batch_lemmas = [instance_metadata["lemmas"] for instance_metadata in metadata]

            # Get a (batch_size, max_sequence_length, num_tags) mask with "1" in
            # tags that are allowed for a given UPOS, and "0" for tags that are
            # disallowed for an even UPOS.
            batch_upos_constraint_mask = self.get_upos_constraint_mask(batch_upos_tags=batch_upos_tags,
                                                                       batch_lemmas=batch_lemmas)

            constrained_logits = util.replace_masked_values(logits,
                                                            batch_upos_constraint_mask,
                                                            -1e32)
        else:
            constrained_logits = logits


        best_paths = self.crf.viterbi_tags(constrained_logits, mask)
        # Just get the tags and ignore the score.
        predicted_tags = [x for x, y in best_paths]

        output = {
                "mask": mask,
                "tags": predicted_tags,
                "tokens": [instance_metadata["tokens"] for instance_metadata in metadata]
        }

        if self.use_upos_constraints:
            output["constrained_logits"] = constrained_logits
            output["upos_tags"] = batch_upos_tags

        if tags is not None:
            # Add negative log-likelihood as loss
            log_likelihood = self.crf(constrained_logits, tags, mask)
            output["loss"] = -log_likelihood

            # Represent viterbi tags as "class probabilities" that we can
            # feed into the metrics
            class_probabilities = constrained_logits * 0.
            for i, instance_tags in enumerate(predicted_tags):
                for j, tag_id in enumerate(instance_tags):
                    class_probabilities[i, j, tag_id] = 1

            for metric in self.metrics.values():
                metric(class_probabilities, tags, mask.float())
        return output

    @overrides
    def decode(self, output_dict: Dict[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
        """
        Converts the tag ids to the actual tags.
        ``output_dict["tags"]`` is a list of lists of tag_ids,
        so we use an ugly nested list comprehension.
        """
        output_dict["tags"] = [
                [self.vocab.get_token_from_index(tag, namespace=self.label_namespace)
                 for tag in instance_tags]
                for instance_tags in output_dict["tags"]]

        return output_dict

    @overrides
    def get_metrics(self, reset: bool = False) -> Dict[str, float]:
        metrics_to_return = {metric_name: metric.get_metric(reset) for
                             metric_name, metric in self.metrics.items()}
        return metrics_to_return

    def get_upos_constraint_mask(self,
                                 batch_upos_tags: List[List[str]],
                                 batch_lemmas: List[List[str]]):
        """
        Given POS tags and lemmas for a batch, return a mask of shape
        (batch_size, max_sequence_length, num_tags) mask with "1" in
        tags that are allowed for a given UPOS, and "0" for tags that are
        disallowed for a given UPOS.

        Parameters
        ----------
        batch_upos_tags: ``List[List[str]]``, required
            UPOS tags for a batch.
        batch_lemmas: ``List[List[str]]``, required
            Lemmas for a batch.

        Returns
        -------
        ``Tensor``, shape (batch_size, max_sequence_length, num_tags)
            A mask over the logits, with 1 in positions where a tag is allowed
            for its UPOS and 0 in positions where a tag is allowed for its UPOS.
        """
        # TODO(nfliu): this is pretty inefficient, maybe there's someway to make it batched?
        # Shape: (batch_size, max_sequence_length, num_tags)
        upos_constraint_mask = torch.ones(len(batch_upos_tags),
                                          len(max(batch_upos_tags, key=len)),
                                          self.num_tags,
                                          device=next(self.tag_projection_layer.parameters()).device) * -1e32
        # Iterate over the batch
        for example_index, (example_upos_tags, example_lemmas) in enumerate(
                zip(batch_upos_tags, batch_lemmas)):
            # Shape of example_constraint_mask: (max_sequence_length, num_tags)
            # Iterate over the upos tags for the example
            example_constraint_mask = upos_constraint_mask[example_index]
            for timestep_index, (timestep_upos_tag, timestep_lemma) in enumerate(  # pylint: disable=unused-variable
                    example_upos_tags):
                # Shape of timestep_constraint_mask: (num_tags,)
                example_constraint_mask[timestep_index] = self._upos_to_label_mask[timestep_upos_tag]
                # Add exceptions for lemmas
                # if lemma is "be", then (upos AUX, lexcat V) is an allowed pairing.
                # if lemma is "versus", then (upos ADP, lc CCONJ) is an alloewd pairing.
                # TODO(nfliu): fill in 1's for these exceptions.
        return upos_constraint_mask

def get_upos_allowed_lexcats():
    # pylint: disable=too-many-return-statements
    def is_allowed(upos, lexcat):
        if lexcat.endswith('!@'):
            return True
        if upos == lexcat:
            return True
        if (upos, lexcat) in {('NOUN', 'N'), ('PROPN', 'N'), ('VERB', 'V'),
                              ('ADP', 'P'), ('ADV', 'P'), ('SCONJ', 'P'),
                              ('ADP', 'DISC'), ('ADV', 'DISC'), ('SCONJ', 'DISC'),
                              ('PART', 'POSS')}:
            return True
        mismatch_ok = False
        if lexcat.startswith('INF'):
            if upos == "SCONJ":
                mismatch_ok = True
            # INF and PART are only ok if the lemma is "to"
            # elif upos == "PART":
            #     mismatch_ok = True
        # AUX and V are ok only if the lemma is "be"
        # if upos =="AUX" and lexcat == "V":
        #     mismatch_ok = True
        if upos == 'PRON':
            if lexcat in ('PRON', 'PRON.POSS'):
                mismatch_ok = True
        if lexcat == 'ADV':
            if upos in ('ADV', 'PART'):
                mismatch_ok = True
        # ADP and CCONJ are only ok if the lemma is "versus"
        # if upos == 'ADP' and lexcat == 'CCONJ':
        #     mismatch_ok = True
        return mismatch_ok

    allowed_combinations = {}
    for lexcat in ALL_LEXCATS:
        for universal_pos in ALL_UPOS:
            if is_allowed(universal_pos, lexcat):
                if universal_pos not in allowed_combinations:
                    allowed_combinations[universal_pos] = set()
                allowed_combinations[universal_pos].add(lexcat)
    logger.info("Allowed lexcats for each UPOS:")
    json_allowed_combinations = {upos: sorted(list(lexcats)) for
                                 upos, lexcats in allowed_combinations.items()}
    logger.info(json.dumps(json_allowed_combinations, indent=2))
    return allowed_combinations

def streusle_allowed_transitions(labels: Dict[int, str]) -> List[Tuple[int, int]]:
    """
    Given labels, returns the allowed transitions when tagging STREUSLE lextags. It will
    additionally include transitions for the start and end states, which are used
    by the conditional random field.

    Parameters
    ----------
    labels : ``Dict[int, str]``, required
        A mapping {label_id -> label}. Most commonly this would be the value from
        Vocabulary.get_index_to_token_vocabulary()

    Returns
    -------
    ``List[Tuple[int, int]]``
        The allowed transitions (from_label_id, to_label_id).
    """
    num_labels = len(labels)
    start_tag = num_labels
    end_tag = num_labels + 1
    labels_with_boundaries = list(labels.items()) + [(start_tag, "START"), (end_tag, "END")]

    allowed = []
    for from_label_index, from_label in labels_with_boundaries:
        if from_label in ("START", "END"):
            from_tag = from_label
        else:
            from_tag = from_label.split("-")[0]
        for to_label_index, to_label in labels_with_boundaries:
            if to_label in ("START", "END"):
                to_tag = to_label
            else:
                to_tag = to_label.split("-")[0]
            if is_streusle_transition_allowed(from_tag, to_tag):
                allowed.append((from_label_index, to_label_index))
    return allowed

def is_streusle_transition_allowed(from_tag: str,
                                   to_tag: str):
    """
    Given a constraint type and strings ``from_tag`` and ``to_tag`` that
    represent the origin and destination of the transition, return whether
    the transition is allowed under the STREUSLE tagging scheme.

    Parameters
    ----------
    from_tag : ``str``, required
        The tag that the transition originates from. For example, if the
        label is ``I~-V-v.cognition``, the ``from_tag`` is ``I~``.
    to_tag : ``str``, required
        The tag that the transition leads to. For example, if the
        label is ``I~-V-v.cognition``, the ``to_tag`` is ``I~``.

    Returns
    -------
    ``bool``
        Whether the transition is allowed under the given ``constraint_type``.
    """
    # pylint: disable=too-many-return-statements
    if from_tag not in ('O', 'B', 'I_', 'I~', 'o', 'b', 'i_', 'i~', 'START', 'END'):
        raise ValueError("Got invalid from_tag {}".format(from_tag))
    if to_tag not in ('O', 'B', 'I_', 'I~', 'o', 'b', 'i_', 'i~', 'START', 'END'):
        raise ValueError("Got invalid to_tag {}".format(to_tag))

    if to_tag == "START" or from_tag == "END":
        # Cannot transition into START or from END
        return False

    if from_tag == "START":
        return to_tag in ('O', 'B')
    if to_tag == "END":
        return from_tag in ('O', 'I_', 'I~')
    return any([
            # B can transition to o-*, b-*, I_-*, or I~-*
            # o can transition to o-*, b-*, I_-*, or I~-*
            from_tag in ('B', 'o') and to_tag in ('o', 'b', 'I_', 'I~'),
            # b can transition to i_ or i~, but only if the entity tags match
            from_tag in ('b',) and to_tag in ('i_', 'i~'),
            # O can transition to O-*, B-*, or END
            from_tag in ('O',) and to_tag in ('O', 'B'),
            # I_, I~ can transition to all tags except i_-* or i~-*
            from_tag in ('I_', 'I~') and to_tag not in ('i_', 'i~'),
            # i_, i~ can transition to all tags except O, B
            from_tag in ('i_', 'i~') and to_tag not in ('O', 'B'),
    ])
