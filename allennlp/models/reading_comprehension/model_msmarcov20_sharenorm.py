import logging
from typing import Any, Dict, List, Optional

import math

import torch
from torch.autograd import Variable
from torch.nn.functional import nll_loss, relu

from allennlp.common import Params
from allennlp.common.checks import check_dimensions_match
from allennlp.data import Vocabulary
from allennlp.models.model import Model
from allennlp.modules import Highway, MatrixAttention, FeedForward
from allennlp.modules import Seq2SeqEncoder, SimilarityFunction, TimeDistributed, TextFieldEmbedder
from allennlp.modules.tri_linear_attention import TriLinearAttention
from allennlp.nn import util, InitializerApplicator, RegularizerApplicator
from allennlp.training.metrics import BooleanAccuracy, CategoricalAccuracy, SquadEmAndF1, Rouge


logger = logging.getLogger(__name__)  # pylint: disable=invalid-name

@Model.register("model-msmarcov20-sharenorm")
class ModelMSMARCO(Model):
    def __init__(self, vocab: Vocabulary,
                 text_field_embedder: TextFieldEmbedder,
                 phrase_layer: Seq2SeqEncoder,
                 attention_similarity_function: SimilarityFunction,
                 residual_encoder: Seq2SeqEncoder,
                 span_start_encoder: Seq2SeqEncoder,
                 span_end_encoder: Seq2SeqEncoder,
                 dropout: float = 0.2,
                 mask_lstms: bool = True,
                 initializer: InitializerApplicator = InitializerApplicator(),
                 regularizer: Optional[RegularizerApplicator] = None) -> None:
        super(ModelMSMARCO, self).__init__(vocab, regularizer)

        self._text_field_embedder = text_field_embedder
        self._phrase_layer = phrase_layer
        self._residual_encoder = residual_encoder
        self._span_end_encoder = span_end_encoder
        self._span_start_encoder = span_start_encoder

        encoding_dim = phrase_layer.get_output_dim()
        self._span_start_predictor = TimeDistributed(torch.nn.Linear(encoding_dim, 1))

        span_end_encoding_dim = span_end_encoder.get_output_dim()
        self._span_end_predictor = TimeDistributed(torch.nn.Linear(encoding_dim, 1))
        self._no_answer_predictor = TimeDistributed(torch.nn.Linear(encoding_dim, 1))

        self._matrix_attention = TriLinearAttention(encoding_dim)
        self._self_matrix_attention = TriLinearAttention(encoding_dim)
        self._linear_layer = TimeDistributed(torch.nn.Linear(4*encoding_dim, encoding_dim))
        self._residual_linear_layer = TimeDistributed(torch.nn.Linear(3*encoding_dim, encoding_dim))
        
        #self._w_x = torch.nn.Parameter(torch.Tensor(encoding_dim))
        #self._w_y = torch.nn.Parameter(torch.Tensor(encoding_dim))
        #self._w_xy = torch.nn.Parameter(torch.Tensor(encoding_dim))

        #std = math.sqrt(6 / (encoding_dim + 1))
        #self._w_x.data.uniform_(-std, std)
        #self._w_y.data.uniform_(-std, std)
        #self._w_xy.data.uniform_(-std, std)

        self._squad_metrics = SquadEmAndF1()
        self._rouge_metric = Rouge()
        if dropout > 0:
            self._dropout = torch.nn.Dropout(p=dropout)
        else:
            self._dropout = lambda x: x
        self._mask_lstms = mask_lstms

        initializer(self)
        self._ite = 0

    def forward(self,  # type: ignore
                question: Dict[str, torch.LongTensor],
                passage: Dict[str, torch.LongTensor],
                span_start: torch.LongTensor = None,
                span_end: torch.LongTensor = None,
                metadata: List[Dict[str, Any]] = None) -> Dict[str, torch.Tensor]:
        # pylint: disable=arguments-differ
        embedded_passage = self._text_field_embedder(passage)
        passage_mask = util.get_text_field_mask(passage, 1).float()
        # get params
        cuda_device = embedded_passage.get_device()
        batch_size, num_passage, passage_length, embedding_dim = embedded_passage.size()
        # when training, select randomly 2 passages from 4 passages each epoch
        if self.training:
            num_passage = 2
            probs = torch.Tensor([1, 1, 1]).unsqueeze(0).expand(batch_size, 3)
            indices = torch.multinomial(probs, 1) + 1
            zeros_tensor = torch.zeros(batch_size).long()
            indices = Variable(torch.cat([zeros_tensor.unsqueeze(-1), indices], 1).cuda(cuda_device))
            embedded_passage = torch.gather(embedded_passage, 1, indices.unsqueeze(-1).unsqueeze(-1).expand(batch_size, num_passage, passage_length, embedding_dim))
            passage_mask = torch.gather(passage_mask, 1, indices.unsqueeze(-1).expand(batch_size, num_passage, passage_length)) 
        # Shape: (batch_size*num_passage, passage_length, embedding_dim)
        embedded_passage = embedded_passage.view(-1, passage_length, embedding_dim)
        embedded_passage = self._dropout(embedded_passage)
        # Shape: (batch_size*num_passage, passage_length)
        passage_mask = passage_mask.view(-1, passage_length)
        # Shape: (batch_size, question_length, embedding_dim)
        embedded_question = self._text_field_embedder(question)
        # Shape: (batch_size*num_passage, question_length)
        embedded_question = embedded_question.unsqueeze(1).expand(-1, num_passage, -1, -1).contiguous().view(batch_size*num_passage, -1, embedding_dim)
        embedded_question = self._dropout(embedded_question)
        # Shape: (batch_size, question_length)
        question_mask = util.get_text_field_mask(question).float()
        # Shape: (batch_size*num_passage, question_length)
        question_mask = question_mask.unsqueeze(1).expand(-1, num_passage, -1).contiguous().view(batch_size*num_passage, -1)
        # lstm masks
        question_lstm_mask = question_mask if self._mask_lstms else None
        passage_lstm_mask = passage_mask if self._mask_lstms else None

        # Shape: (batch_size*num_passage, -1, encoding_dim)
        encoded_question = self._dropout(self._phrase_layer(embedded_question, question_lstm_mask))
        encoded_passage = self._dropout(self._phrase_layer(embedded_passage, passage_lstm_mask))
        encoding_dim = encoded_question.size(-1)
        # Shape: (batch_size*num_passage, passage_length, question_length)
        passage_question_similarity = self._matrix_attention(encoded_passage, encoded_question)
        # Shape: (batch_size*num_passage, passage_length, question_length)
        passage_question_attention = util.last_dim_softmax(passage_question_similarity, question_mask)
        # Shape: (batch_size_num_passage, passage_length, encoding_dim)
        passage_question_vectors = util.weighted_sum(encoded_question, passage_question_attention)
        # We replace masked values with something really negative here, so they don't affect the
        # max below.
        masked_similarity = util.replace_masked_values(passage_question_similarity,
                                                       question_mask.unsqueeze(1),
                                                       -1e7)
        # Shape: (batch_size*num_passage, passage_length)
        question_passage_similarity = masked_similarity.max(dim=-1)[0].squeeze(-1)
        # Shape: (batch_size*num_passage, passage_length)
        question_passage_attention = util.masked_softmax(question_passage_similarity, passage_mask)
        # Shape: (batch_size*num_passage, encoding_dim)
        question_passage_vector = util.weighted_sum(encoded_passage, question_passage_attention)
        # Shape: (batch_size*num_passage, passage_length, encoding_dim)
        tiled_question_passage_vector = question_passage_vector.unsqueeze(1).expand(batch_size*num_passage,
                                                                                    passage_length,
                                                                                    encoding_dim)

        # Shape: (batch_size*num_passage, passage_length, encoding_dim * 4)
        final_merged_passage = torch.cat([encoded_passage,
                                          passage_question_vectors,
                                          encoded_passage * passage_question_vectors,
                                          encoded_passage * tiled_question_passage_vector],
                                         dim=-1)
        # Shape: (batch_size*num_passage, passage_length, encoding_dim)
        question_attended_passage = relu(self._linear_layer(final_merged_passage))
        # attach residual self-attention layer
        # Shape: (batch_size*num_passage, passage_length, encoding_dim)
        residual_passage = self._dropout(self._residual_encoder(self._dropout(question_attended_passage), passage_lstm_mask))
        # self-attention mask
        mask = passage_mask.resize(batch_size*num_passage, passage_length, 1) * passage_mask.resize(batch_size*num_passage, 1, passage_length)
        self_mask = Variable(torch.eye(passage_length, passage_length).cuda(cuda_device)).resize(1, passage_length, passage_length)
        mask = mask * (1 - self_mask)
        # Shape: (batch_size*num_passage, passage_length, passage_length)
        passage_self_similarity = self._self_matrix_attention(residual_passage, residual_passage)
        # Shape: (batch_size*num_passage, passage_length, passage_length)
        passage_self_attention = util.last_dim_softmax(passage_self_similarity, mask)
        # Shape: (batch_size*num_passage, passage_length, encoding_dim)
        passage_vectors = util.weighted_sum(residual_passage, passage_self_attention)
        # Shape: (batch_size*num_passage, passage_length, encoding_dim * 3)
        merged_passage = torch.cat([residual_passage,
                                    passage_vectors,
                                    residual_passage * passage_vectors],
                                    dim=-1)
        # Shape: (batch_size*num_passage, passage_length, encoding_dim)
        self_attended_passage = relu(self._residual_linear_layer(merged_passage))
        # Shape: (batch_size*num_passage, passage_length, encoding_dim)
        mixed_passage = question_attended_passage + self_attended_passage
        # add dropout
        mixed_passage = self._dropout(mixed_passage)
        
        # Shape: (batch_size*num_passage, passage_length, encoding_dim)
        encoded_span_start = self._dropout(self._span_start_encoder(mixed_passage, passage_lstm_mask))
        span_start_logits = self._span_start_predictor(encoded_span_start).squeeze(-1)
        span_start_probs = util.masked_softmax(span_start_logits, passage_mask)
        # Shape: (batch_size*num_passage, passage_length, encoding_dim * 2) 
        concatenated_passage = torch.cat([mixed_passage, encoded_span_start], dim=-1)
        # Shape: (batch_size*num_passage, passage_length, encoding_dim)
        encoded_span_end = self._dropout(self._span_end_encoder(concatenated_passage, passage_lstm_mask))
        span_end_logits = self._span_end_predictor(encoded_span_end).squeeze(-1)
        span_end_probs = util.masked_softmax(span_end_logits, passage_mask)
        # Shape: (batch_size*num_passage, passage_length)
        span_start_logits = util.replace_masked_values(span_start_logits, passage_mask, -1e7)
        span_end_logits = util.replace_masked_values(span_end_logits, passage_mask, -1e7)
        
        output_dict = {
                "passage_question_attention": passage_question_attention,
                "span_start_logits": span_start_logits,
                "span_start_probs": span_start_probs,
                "span_end_logits": span_end_logits,
                "span_end_probs": span_end_probs,
                }
        # when training, we need to merge all the paragraphs in the same context before computing loss
        if span_start is not None:
            span_start_logits = span_start_logits.view(batch_size, num_passage, passage_length).view(batch_size, -1)
            span_end_logits = span_end_logits.view(batch_size, num_passage, passage_length).view(batch_size, -1)
            passage_mask = passage_mask.view(batch_size, num_passage, passage_length).view(batch_size, -1)
            loss = nll_loss(util.masked_log_softmax(span_start_logits, passage_mask), span_start.squeeze(-1))
            loss += nll_loss(util.masked_log_softmax(span_end_logits, passage_mask), span_end.squeeze(-1))
        else:
            loss = Variable(torch.Tensor([0]).cuda(cuda_device))
        output_dict["loss"] = loss
        # when validating, compute the ROUGE score
        if metadata is not None:
            # find best span of all the paragraphs
            best_span = self.get_best_span(span_start_logits, span_end_logits)
            best_span = best_span.view(batch_size, num_passage, 3)
            # extract answer for computing Rouge, F1 and EM
            output_dict['best_span_str'] = []
            question_tokens = []
            for i in range(batch_size):
                question_tokens.append(metadata[i]['question_tokens'])
                all_passages = metadata[i]['all_passages']
                passage_offsets = metadata[i]['passage_offsets']
                # get the paragraph with highest confidence span
                _, max_id = torch.max(best_span[i, :, 2], dim=0)
                max_id = int(max_id)
                # extract answer text
                predicted_span = tuple(best_span[i, max_id].data.cpu().numpy())
                start_offset = passage_offsets[max_id][int(predicted_span[0])][0]
                end_offset = passage_offsets[max_id][int(predicted_span[1])][1]
                best_span_string = all_passages[max_id][start_offset:end_offset]
                output_dict['best_span_str'].append(best_span_string)
                answer_texts = metadata[i].get('answer_texts', [])
                if answer_texts:
                    self._ite += 1
                    if (self._ite % 100 == 0):
                        print("%s || %s" % (best_span_string, answer_texts))
                    self._squad_metrics(best_span_string, answer_texts)
                    self._rouge_metric(best_span_string, answer_texts)
            output_dict['question_tokens'] = question_tokens
        return output_dict

    def get_metrics(self, reset: bool = False) -> Dict[str, float]:
        exact_match, f1_score = self._squad_metrics.get_metric(reset)
        return {
                'em': exact_match,
                'f1': f1_score,
                'rouge': self._rouge_metric.get_metric(reset),
                }

    @staticmethod
    def get_best_span(span_start_logits: Variable, span_end_logits: Variable) -> Variable:
        if span_start_logits.dim() != 2 or span_end_logits.dim() != 2:
            raise ValueError("Input shapes must be (batch_size, passage_length)")
        batch_size, passage_length = span_start_logits.size()
        max_span_log_prob = [-1e20] * batch_size
        span_start_argmax = [0] * batch_size
        best_word_span = Variable(span_start_logits.data.new()
                                  .resize_(batch_size, 3).fill_(0))

        span_start_logits = span_start_logits.data.cpu().numpy()
        span_end_logits = span_end_logits.data.cpu().numpy()

        for b in range(batch_size):  # pylint: disable=invalid-name
            for j in range(passage_length):
                val1 = span_start_logits[b, span_start_argmax[b]]
                if val1 < span_start_logits[b, j]:
                    span_start_argmax[b] = j
                    val1 = span_start_logits[b, j]

                val2 = span_end_logits[b, j]

                if val1 + val2 > max_span_log_prob[b]:
                    best_word_span[b, 0] = span_start_argmax[b]
                    best_word_span[b, 1] = j
                    best_word_span[b, 2] = float(val1 + val2)
                    max_span_log_prob[b] = val1 + val2
        return best_word_span

    @classmethod
    def from_params(cls, vocab: Vocabulary, params: Params) -> 'ModelMSMARCO':
        embedder_params = params.pop("text_field_embedder")
        text_field_embedder = TextFieldEmbedder.from_params(vocab, embedder_params)
        #num_highway_layers = params.pop_int("num_highway_layers")
        phrase_layer = Seq2SeqEncoder.from_params(params.pop("phrase_layer"))
        similarity_function = SimilarityFunction.from_params(params.pop("similarity_function"))
        residual_encoder = Seq2SeqEncoder.from_params(params.pop("residual_encoder"))
        span_start_encoder = Seq2SeqEncoder.from_params(params.pop("span_start_encoder"))
        span_end_encoder = Seq2SeqEncoder.from_params(params.pop("span_end_encoder"))
        #feed_forward = FeedForward.from_params(params.pop("feed_forward"))
        dropout = params.pop_float('dropout', 0.2)

        initializer = InitializerApplicator.from_params(params.pop('initializer', []))
        regularizer = RegularizerApplicator.from_params(params.pop('regularizer', []))

        mask_lstms = params.pop_bool('mask_lstms', True)
        params.assert_empty(cls.__name__)
        return cls(vocab=vocab,
                   text_field_embedder=text_field_embedder,
            #       num_highway_layers=num_highway_layers,
                   phrase_layer=phrase_layer,
                   attention_similarity_function=similarity_function,
                   residual_encoder=residual_encoder,
                   span_start_encoder=span_start_encoder,
                   span_end_encoder=span_end_encoder,
                   dropout=dropout,
                   mask_lstms=mask_lstms,
                   initializer=initializer,
                   regularizer=regularizer)
