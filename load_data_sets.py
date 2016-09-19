import itertools
import gzip
import numpy as np
import os
import struct
import sys

from features import DEFAULT_FEATURES
import go
import sgf_wrapper
import utils

# Number of data points to store in a chunk on disk
CHUNK_SIZE = 4096
CHUNK_HEADER_FORMAT = "iii?"
CHUNK_HEADER_SIZE = struct.calcsize(CHUNK_HEADER_FORMAT)

def take_n(n, iterator):
    result = []
    try:
        for i in range(n):
            result.append(next(iterator))
    except StopIteration:
        pass
    finally:
        return result

def iter_chunks(chunk_size, iterator):
    while True:
        next_chunk = take_n(chunk_size, iterator)
        # If len(iterable) % chunk_size == 0, don't return an empty chunk.
        if next_chunk:
            yield next_chunk
        else:
            break

def make_onehot(dense_labels, num_classes):
    dense_labels = np.fromiter(dense_labels, dtype=np.int16)
    num_labels = dense_labels.shape[0]
    index_offset = np.arange(num_labels) * num_classes
    labels_one_hot = np.zeros((num_labels, num_classes), dtype=np.int16)
    labels_one_hot.flat[index_offset + dense_labels.ravel()] = 1
    return labels_one_hot

def find_sgf_files(*dataset_dirs):
    for dataset_dir in dataset_dirs:
        full_dir = os.path.join(os.getcwd(), dataset_dir)
        dataset_files = [os.path.join(full_dir, name) for name in os.listdir(full_dir)]
        for f in dataset_files:
            if os.path.isfile(f) and f.endswith(".sgf"):
                yield f

def get_positions_from_sgf(file):
    with open(file) as f:
        sgf = sgf_wrapper.SgfWrapper(f.read())
        for position_w_context in sgf.get_main_branch():
            if position_w_context.is_usable():
                yield position_w_context

def extract_features(positions):
    num_positions = len(positions)
    output = np.zeros([num_positions, go.N, go.N, DEFAULT_FEATURES.planes], dtype=np.float32)
    for i, pos in enumerate(positions):
        output[i] = DEFAULT_FEATURES.extract(pos)
    return output

def split_test_training(positions_w_context, est_num_positions):
    desired_test_size = 10**5
    if est_num_positions < 2 * desired_test_size:
        positions_w_context = list(positions_w_context)
        test_size = len(positions_w_context) // 3
        return positions_w_context[:test_size], [positions_w_context[test_size:]]
    else:
        test_chunk = take_n(desired_test_size, positions_w_context)
        training_chunks = iter_chunks(CHUNK_SIZE, positions_w_context)
        return test_chunk, training_chunks


class DataSet(object):
    def __init__(self, pos_features, next_moves, results, is_test=False):
        self.pos_features = pos_features
        self.next_moves = next_moves
        self.results = results
        self.is_test = is_test
        assert pos_features.shape[0] == next_moves.shape[0], "Didn't pass in same number of pos_features and next_moves."
        self.data_size = pos_features.shape[0]
        self.board_size = pos_features.shape[1]
        self.input_planes = pos_features.shape[-1]
        self._index_within_epoch = 0

    def get_batch(self, batch_size):
        assert batch_size < self.data_size
        if self._index_within_epoch + batch_size > self.data_size:
            # Shuffle the data and start over
            perm = np.arange(self.data_size)
            np.random.shuffle(perm)
            self.pos_features = self.pos_features[perm]
            self.next_moves = self.next_moves[perm]
            self._index_within_epoch = 0
        start = self._index_within_epoch
        end = start + batch_size
        self._index_within_epoch += batch_size
        return self.pos_features[start:end], self.next_moves[start:end]

    @staticmethod
    def from_positions_w_context(positions_w_context, is_test=False):
        positions, next_moves, results = zip(*positions_w_context)
        extracted_features = extract_features(positions)
        encoded_moves = make_onehot(map(utils.flatten_coords, next_moves), go.N ** 2)
        return DataSet(extracted_features, encoded_moves, results, is_test=is_test)

    def write(self, filename):
        header_bytes = struct.pack(CHUNK_HEADER_FORMAT, self.data_size, self.board_size, self.input_planes, self.is_test)
        position_bytes = self.pos_features.tostring()
        next_move_bytes = self.next_moves.tostring()
        with gzip.open(filename, "wb") as f:
            f.write(header_bytes)
            f.write(position_bytes)
            f.write(next_move_bytes)

    @staticmethod
    def read(filename):
        with gzip.open(filename, "rb") as f:
            header_bytes = f.read(CHUNK_HEADER_SIZE)
            data_size, board_size, input_planes, is_test = struct.unpack(CHUNK_HEADER_FORMAT, header_bytes)
            position_bytes = f.read(data_size * board_size * board_size * input_planes * 4)
            next_move_bytes = f.read(data_size * board_size * board_size * 2)
            pos_features = np.fromstring(position_bytes, dtype=np.float32).reshape(data_size, board_size, board_size, input_planes)
            next_moves = np.fromstring(next_move_bytes, dtype=np.int16).reshape(data_size, board_size * board_size)
        return DataSet(pos_features, next_moves, [], is_test=is_test)

def process_raw_data(*dataset_dirs, processed_dir="processed_data"):
    sgf_files = list(find_sgf_files(*dataset_dirs))
    print("%s sgfs found." % len(sgf_files), file=sys.stderr)
    est_num_positions = len(sgf_files) * 200 # about 200 moves per game
    print("Estimated number of chunks: %s" % (est_num_positions // CHUNK_SIZE), file=sys.stderr)
    positions_w_context = itertools.chain(*map(get_positions_from_sgf, sgf_files))

    test_chunk, training_chunks = split_test_training(positions_w_context, est_num_positions)
    print("Allocating %s positions as test; remainder as training" % len(test_chunk), file=sys.stderr)

    print("Writing test chunk")
    test_dataset = DataSet.from_positions_w_context(test_chunk, is_test=True)
    test_filename = os.path.join(processed_dir, "test.chunk.gz")
    test_dataset.write(test_filename)

    training_datasets = map(DataSet.from_positions_w_context, training_chunks)
    for i, train_dataset in enumerate(training_datasets):
        if i % 10 == 0:
            print("Writing training chunk %s" % i)
        train_filename = os.path.join(processed_dir, "train%s.chunk.gz" % i)
        train_dataset.write(train_filename)
    print("%s chunks written" % (i+1))
