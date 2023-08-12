# import sys, os
import argparse
from enum import IntEnum
from pathlib import Path
import logging
import json
from tqdm import tqdm


import torch
from torch.utils.data import Dataset, Subset, ConcatDataset


from model_training.utils.utils import (
    get_dataset,
    read_yamls,
    _strtobool,
)
from model_training.custom_datasets.formatting import (
    DatasetEntryLm,
    DatasetEntrySft,
    Role,
)


from tokenizer import build_tokenizer
import indexed_dataset

logger = logging.getLogger(__name__)


class IntRole(IntEnum):
    System = 0
    Prompter = 1
    Assistant = 2


class Encoder(object):
    def __init__(self, args):
        self.args = args
        self.tokenizer = build_tokenizer(self.args)

    def encode_text(self, text: str) -> list[int]:
        return self.tokenizer.tokenize(text)

    def decode(self, tokens: list[int]) -> str:
        return self.tokenizer.detokenize(tokens)

    @property
    def special_tokens(self) -> dict:
        return self.tokenizer._special_tokens


class DatasetWriter:
    def __init__(
        self,
        filename_prefix: str,
        vocab_size: int,
        dataset_impl: str = "mmap",
        feature: str = "text",
    ):
        self.bin_filename = f"{filename_prefix}-{feature}.bin"
        self.idx_filename = f"{filename_prefix}-{feature}.idx"
        self.builder = indexed_dataset.make_builder(
            self.bin_filename, impl=dataset_impl, vocab_size=vocab_size
        )

    def add_item(self, tokenized_item):
        self.builder.add_item(torch.IntTensor(tokenized_item))

    def finalize(self):
        self.builder.finalize(self.idx_filename)


def format_pairs(pairs: list[str] | tuple[str]) -> tuple[list[str], list[int]]:
    assert isinstance(pairs, list) or isinstance(pairs, tuple)
    role_names = ("user", "assistant")
    role_ids = (1, 2)
    return [
        f"<|im_start|>{role_names[i%2]}\n{pairs[i]}<|im_end|>\n"
        for i in range(len(pairs))
    ], [role_ids[i % 2] for i in range(len(pairs))]


def format_sft_entry(entry: DatasetEntrySft) -> tuple[list[str], list[int]]:
    turns = []
    roles = []
    if entry.system_message and len(entry.system_message) > 0:
        turns.append(f"<|im_start|>system\n{entry.system_message}<|im_end|>\n")
        roles.append(IntRole.System.value)  # 0
    for m in entry.conversation:
        if m.role == Role.prompter:
            turns.append(f"<|im_start|>user\n{m.text}<|im_end|>\n")
            roles.append(IntRole.Prompter.value)  # 1
        elif m.role == Role.assistant:
            turns.append(f"<|im_start|>assistant\n{m.text}<|im_end|>\n")
            roles.append(IntRole.Assistant.value)  # 2
    return turns, roles


def format_conversation(messages) -> str:
    if isinstance(messages, DatasetEntrySft):
        return format_sft_entry(messages)
    elif isinstance(messages, DatasetEntryLm):
        return messages.text, [3]
    else:
        return format_pairs(messages)


class TokenStats:
    def __init__(self, name: str, total_samples: int):
        self.name = name
        self.skipped_samples = 0
        self.skipped_tokens = 0
        self.total_samples = total_samples
        self.min_tokens = None
        self.max_tokens = 0
        self.accepted_samples = 0
        self.accepted_tokens = 0

    @property
    def processed_samples(self) -> int:
        return self.accepted_samples + self.skipped_samples

    def skip(self, tokens: list[int]) -> None:
        self.skipped_samples += 1
        self.skipped_tokens = len(tokens)

    def add(self, tokens: list[int]) -> None:
        l = len(tokens)
        self.accepted_samples += 1
        self.accepted_tokens += l
        if self.min_tokens is None or self.min_tokens > l:
            self.min_tokens = l
        if self.max_tokens < l:
            self.max_tokens = l


def tokenize_dataset(
    output_dir: Path,
    filename_prefix: str,
    dataset: Dataset,
    encoder: Encoder,
    dataset_impl: str,
    max_count: int | None = None,
    min_assistant_tokens: int | None = None,
    check_tokenization: bool = True,
    write_json: bool = False,
):
    full_prefix = str(output_dir / filename_prefix)

    token_writer = None
    role_writer = None
    jsonl_file = None

    total_stats = TokenStats("total", len(dataset))
    per_dataset_stats: list[TokenStats] = []
    cumulative_sizes: list[int] = []

    if isinstance(dataset, ConcatDataset):
        per_dataset_stats
        s = 0
        for d in dataset.datasets:
            if isinstance(d, Subset):
                if hasattr(d.dataset, "name"):
                    name = d.dataset.name
                else:
                    name = f"Subset of {type(d.dataset).__name__}"
            else:
                if hasattr(d, "name"):
                    name += d.name
                else:
                    name = type(d).__name__

            per_dataset_stats.append(TokenStats(name, len(d)))
            s += len(d)
            cumulative_sizes.append(s)

    try:
        token_writer = DatasetWriter(
            filename_prefix=full_prefix,
            dataset_impl=dataset_impl,
            vocab_size=encoder.tokenizer.vocab_size,
            feature="text",
        )

        role_writer = DatasetWriter(
            filename_prefix=full_prefix,
            dataset_impl=dataset_impl,
            vocab_size=16,
            feature="role",
        )

        jsonl_path = Path(full_prefix + ".jsonl")
        if write_json:
            jsonl_file = jsonl_path.open("w", encoding="UTF-8")

        subset_index = 0
        for i, messages in enumerate(tqdm(dataset)):
            if i >= cumulative_sizes[subset_index]:
                subset_index += 1

            if i > 0 and i % 10000 == 0:
                print(
                    f"Accepted: {total_stats.accepted_samples}/{total_stats.processed_samples} ({total_stats.accepted_samples/total_stats.processed_samples:.1%})"
                )

            turns, turn_roles = format_conversation(messages)

            tokens = []
            role_lables = []
            num_assistant_tokens = 0
            for t, r in zip(turns, turn_roles):
                turn_tokens = encoder.encode_text(t)
                turn_role = [r] * len(turn_tokens)
                tokens.extend(turn_tokens)
                if r == IntRole.Assistant:
                    num_assistant_tokens += len(turn_tokens)
                role_lables.extend(turn_role)

            if (
                min_assistant_tokens is not None
                and num_assistant_tokens < min_assistant_tokens
            ):
                total_stats.skip(tokens)
                per_dataset_stats[subset_index].skip(tokens)
                continue

            if check_tokenization:
                x = encoder.encode_text("".join(turns))
                assert x == tokens and len(tokens) == len(role_lables)

            token_writer.add_item(tokens)
            role_writer.add_item(role_lables)

            # update stats
            total_stats.add(tokens)
            per_dataset_stats[subset_index].add(tokens)

            if jsonl_file:
                json.dump({"text": "".join(turns)}, jsonl_file)
                jsonl_file.write("\n")

            if max_count and total_stats.accepted_samples >= max_count:
                break
    finally:
        if token_writer:
            token_writer.finalize()
        if role_writer:
            role_writer.finalize()
        if jsonl_file:
            jsonl_file.close()

    print(f"# Stats for {full_prefix}*")

    for stats in per_dataset_stats:
        print(f"## Stats for '{stats.name}' ({stats.total_samples} samples)")
        print("-----------------")
        print(
            f"  Accepted: {stats.accepted_samples}/{stats.processed_samples} ({stats.accepted_samples/stats.processed_samples:.1%})"
        )
        print(f"  Accepted tokens: {stats.accepted_tokens}")
        print(
            f"  Skipped: {stats.skipped_samples} ({stats.skipped_samples/stats.processed_samples:.1%})"
        )
        print(f"  Min tokens per sample: {stats.min_tokens}")
        print(f"  Max tokens per sample: {stats.max_tokens}")
        print(
            f"  Avg tokens per sample: {stats.accepted_tokens/stats.accepted_samples}"
        )
        print("-----------------")
        print()


def parse_args():
    parser = argparse.ArgumentParser()
    group = parser.add_argument_group(title="configuration")
    group.add_argument(
        "--configs",
        nargs="+",
        required=True,
        help="Multiple configs can be passed to set different options.",
    )
    group.add_argument(
        "--output_dir",
        type=str,
        help="Path to binary output file without suffix",
    )
    group.add_argument(
        "--write_json",
        type=_strtobool,
        default=False,
    )

    args, remaining = parser.parse_known_args()

    # load yaml configurations
    conf = {}
    configs = read_yamls("./configs")
    conf.update(configs["defaults"])
    try:
        for name in args.configs:
            if "," in name:
                for n in name.split(","):
                    conf.update(configs[n])
            else:
                conf.update(configs[name])
    except KeyError as e:
        print(f'Error: Section "{e.args[0]}" not found in YAML configuration files.')
        exit(1)

    # override yaml args
    for k, v in vars(args).items():
        if k == "configs" or v is None:
            continue
        conf[k] = v

    parser = argparse.ArgumentParser()
    for key, value in conf.items():
        type_ = type(value) if value is not None else str
        if type_ == bool:
            type_ = _strtobool
        parser.add_argument(f"--{key}", type=type_, default=value)
        # Allow --no-{key}  to remove a configuration value
        parser.add_argument(f"--no-{key}", dest=key, action="store_const", const=None)
    parser.add_argument(
        "--max_count",
        type=int,
        help="Limit number of train/eval examples to process (debug)",
    )

    args = parser.parse_args(remaining)
    args.keep_empty = False
    args.rank = 0
    args.vocab_extra_ids = 0
    args.make_vocab_size_divisible_by = 128
    args.tensor_model_parallel_size = 1
    args.new_tokens = True

    return args


def main():
    """
    Example usage: `python pretokenize.py --output_dir output--configs llama_oasst_top1`
    """
    args = parse_args()
    print("Configuration:")
    for k, v in vars(args).items():
        print(f"{k}: {v}")

    train, evals = get_dataset(args)

    # show dataset stats
    print("Training dataset sizes (before sampling):")
    total = len(train)
    for d in train.datasets:
        if isinstance(d, Subset):
            name = f"Subset of {type(d.dataset).__name__}"
            if hasattr(d.dataset, "name"):
                name += f" ({d.dataset.name})"
        else:
            name = type(d).__name__
            if hasattr(d, "name"):
                name += f" ({d.name})"
        print(f"{name}: {len(d)} ({len(d) / total:.2%})")

    print("Building encoder")
    encoder = Encoder(args)

    output_dir = Path(args.output_dir)

    print(f"Vocab size: {encoder.tokenizer.vocab_size}")
    print(f"Output dir: {args.output_dir} (exists: {output_dir.exists()})")

    output_dir.mkdir(exist_ok=True)

    fn = output_dir / "special_tokens.json"
    with fn.open("w") as f:
        json.dump(encoder.special_tokens, f)

    val = ConcatDataset(evals.values())
    for split_name, ds in zip(["train", "val"], [train, val]):
        tokenize_dataset(
            output_dir=output_dir,
            filename_prefix=f"{args.filename_prefix}-{split_name}",
            dataset=ds,
            encoder=encoder,
            dataset_impl=args.dataset_impl,
            max_count=args.max_count,
            min_assistant_tokens=args.min_assistant_tokens,
            write_json=args.write_json,
        )


if __name__ == "__main__":
    main()
