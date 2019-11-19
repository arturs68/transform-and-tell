import logging
import os
import pickle
import random
from itertools import takewhile
from typing import Dict

import spacy
from allennlp.data.dataset_readers.dataset_reader import DatasetReader
from allennlp.data.fields import MetadataField, TextField
from allennlp.data.instance import Instance
from allennlp.data.token_indexers import TokenIndexer
from allennlp.data.tokenizers import Tokenizer
from overrides import overrides
from PIL import Image
from pymongo import MongoClient
from spacy.tokens import Doc
from torchvision.transforms import (CenterCrop, Compose, Normalize, Resize,
                                    ToTensor)
from tqdm import tqdm

from tell.data.fields import ImageField, ListTextField, RareTextField

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


@DatasetReader.register('goodnews_rare')
class RareGoodNewsReader(DatasetReader):
    """Read from the Good News dataset.

    See the repo README for more instruction on how to download the dataset.

    Parameters
    ----------
    tokenizer : ``Tokenizer``
        We use this ``Tokenizer`` for both the premise and the hypothesis.
        See :class:`Tokenizer`.
    token_indexers : ``Dict[str, TokenIndexer]``
        We similarly use this for both the premise and the hypothesis.
        See :class:`TokenIndexer`.
    """

    def __init__(self,
                 tokenizer: Tokenizer,
                 token_indexers: Dict[str, TokenIndexer],
                 image_dir: str,
                 counter_path: str,
                 rare_threshold: int = 10,
                 mongo_host: str = 'localhost',
                 mongo_port: int = 27017,
                 eval_limit: int = 5120,
                 lazy: bool = True) -> None:
        super().__init__(lazy)
        self._tokenizer = tokenizer
        self._token_indexers = token_indexers
        self.client = MongoClient(host=mongo_host, port=mongo_port)
        self.db = self.client.goodnews
        self.image_dir = image_dir
        self.preprocess = Compose([
            # Resize(256), CenterCrop(224),
            ToTensor(),
            Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])])
        self.eval_limit = eval_limit
        random.seed(1234)

        with open(counter_path, 'rb') as f:
            counter = pickle.load(f)['train']['caption']
            self.most_common = dict(takewhile(
                lambda x: x[1] > rare_threshold, counter.most_common()))

    @overrides
    def _read(self, split: str):
        # split can be either train, valid, or test
        if split not in ['train', 'val', 'test']:
            raise ValueError(f'Unknown split: {split}')

        # Setting the batch size is needed to avoid cursor timing out
        # We limit the validation set to 1000
        limit = self.eval_limit if split == 'val' else 0
        sample_cursor = self.db.splits.find({
            'split': {'$eq': split},
        }, no_cursor_timeout=True, limit=limit).batch_size(128)

        for sample in sample_cursor:
            # Find the corresponding article
            article = self.db.articles.find_one({
                '_id': {'$eq': sample['article_id']},
            })

            # Load the image
            image_path = os.path.join(self.image_dir, f"{sample['_id']}.jpg")
            try:
                image = Image.open(image_path)
            except (FileNotFoundError, OSError):
                continue

            yield self.article_to_instance(article, image, sample['image_index'], image_path)

        sample_cursor.close()

    def article_to_instance(self, article, image, image_index, image_path) -> Instance:
        context = article['context'].strip()

        caption = article['images'][image_index]
        caption = caption.strip()

        context_tokens = self._tokenizer.tokenize(context)
        caption_tokens = self._tokenizer.tokenize(caption)

        fields = {
            'context': TextField(context_tokens, self._token_indexers),
            'image': ImageField(image, self.preprocess),
            'caption': RareTextField(caption_tokens, self._token_indexers, context_tokens, self.most_common),
        }

        metadata = {'context': context,
                    'caption': caption,
                    'web_url': article['web_url'],
                    'image_path': image_path}
        fields['metadata'] = MetadataField(metadata)

        return Instance(fields)