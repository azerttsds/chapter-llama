# Copyright (c) Meta Platforms, Inc. and affiliates.
# This software may be used and distributed according to the terms of the Llama 2 Community License Agreement.

import os
import random
from pathlib import Path
from warnings import warn

import idr_torch  # needed to set the distributed environment variables
import numpy as np
import torch
import torch.optim as optim
from accelerate.utils import is_xpu_available
from llama_cookbook.configs import fsdp_config as FSDP_CONFIG
from llama_cookbook.configs import quantization_config as QUANTIZATION_CONFIG
from llama_cookbook.configs import train_config as TRAIN_CONFIG
from llama_cookbook.data.concatenator import ConcatDataset
from llama_cookbook.policies import AnyPrecisionAdamW, apply_fsdp_checkpointing
from llama_cookbook.utils import fsdp_auto_wrap_policy
from llama_cookbook.utils.config_utils import (
    check_fsdp_config,
    generate_dataset_config,
    generate_peft_config,
    get_dataloader_kwargs,
    update_config,
)
from llama_cookbook.utils.dataset_utils import get_custom_data_collator
from llama_cookbook.utils.fsdp_utils import get_policies, hsdp_device_mesh
from llama_cookbook.utils.train_utils import (
    clear_gpu_cache,
    freeze_transformer_layers,
    print_model_size,
    setup_environ_flags,
)
from peft import PeftModel, get_peft_model
from torch.distributed.fsdp import FullyShardedDataParallel as FSDP
from torch.distributed.fsdp import ShardingStrategy
from torch.distributed.fsdp.fully_sharded_data_parallel import CPUOffload
from torch.optim.lr_scheduler import StepLR
from transformers import (
    AutoConfig,
    AutoProcessor,
    AutoTokenizer,
    LlamaForCausalLM,
    MllamaForConditionalGeneration,
)
from transformers.models.llama.modeling_llama import LlamaDecoderLayer
from transformers.models.mllama.modeling_mllama import (
    MllamaSelfAttentionDecoderLayer,
    MllamaVisionEncoderLayer,
)

from src.models.llama_finetune import setup_wandb
from src.models.llama_finetune_vision_train import train_mm
from src.models.utils_tokenizer import tokenize_dialog
from src.utils import RankedLogger

log = RankedLogger(__name__, rank_zero_only=True)


class VidCollator:
    def __init__(self, collator):
        self.collator = collator

    def __call__(self, batch):
        vid_ids = [item.pop("vid_id") for item in batch if "vid_id" in item]

        # Apply the standard collator
        batch = self.collator(batch)

        # Add vid_ids back to the batch
        if len(vid_ids) > 0:
            batch["vid_id"] = vid_ids
        return batch


class Trainer:
    def __init__(self, mm_projector):
        self.model = None
        self.mm_projector = mm_projector

    def fit(self, model_config, datamodule):
        self.model = main(
            dataset_train=datamodule,
            mm_projector=self.mm_projector,
            **model_config,
        )
        return self.model


def main(dataset_train, mm_projector, **kwargs):
    # Update the configuration for the training and sharding process
    train_config, fsdp_config = TRAIN_CONFIG(), FSDP_CONFIG()
    update_config((train_config, fsdp_config), **kwargs)
    # Set the seeds for reproducibility
    if is_xpu_available():
        torch.xpu.manual_seed(train_config.seed)
    torch.manual_seed(train_config.seed)
    random.seed(train_config.seed)
    np.random.seed(train_config.seed)

    if train_config.enable_fsdp:
        # setup()
        # torchrun specific
        local_rank = int(os.environ["LOCAL_RANK"])
        rank = int(os.environ["RANK"])
        os.environ["WORLD_SIZE"] = str(idr_torch.world_size)
        # world_size = int(os.environ["WORLD_SIZE"])

    if torch.distributed.is_initialized():
        if is_xpu_available():
            torch.xpu.set_device(local_rank)
        elif torch.cuda.is_available():
            torch.cuda.set_device(local_rank)
        clear_gpu_cache(local_rank)
        setup_environ_flags(rank)

    wandb_run = None

    if train_config.use_wandb and (not train_config.enable_fsdp or rank == 0):
        wandb_run = setup_wandb(train_config, fsdp_config, **kwargs)

    # setting quantization configs
    bnb_config = None
    if train_config.quantization:
        if type(train_config.quantization) == type(True):  # noqa: E721
            warn(  # noqa: B028
                "Quantization (--quantization) is a boolean, please specify quantization as '4bit' or '8bit'. Defaulting to '8bit' but this might change in the future.",
                FutureWarning,
            )
            train_config.quantization = "8bit"

        if train_config.quantization == "8bit" and train_config.enable_fsdp:
            raise ValueError(
                "8bit quantization is not supported with FSDP, please use 4bit quantization"
            )

        quant_config = QUANTIZATION_CONFIG()
        update_config(quant_config, **kwargs)
        bnb_config = quant_config.create_bnb_config(train_config.quantization)

    # Load the pre-trained model and setup its configuration
    use_cache = False if train_config.enable_fsdp else None
    config = AutoConfig.from_pretrained(train_config.model_name)
    if config.model_type == "mllama":
        is_vision = True
        model = MllamaForConditionalGeneration.from_pretrained(
            train_config.model_name,
            quantization_config=bnb_config,
            attn_implementation="sdpa" if train_config.use_fast_kernels else None,
            device_map=(
                "auto"
                if train_config.quantization and not train_config.enable_fsdp
                else None
            ),
            torch_dtype=torch.float16 if train_config.use_fp16 else torch.bfloat16,
        )
        processor = AutoProcessor.from_pretrained(
            train_config.model_name
            if train_config.tokenizer_name is None
            else train_config.tokenizer_name
        )
        processor.tokenizer.padding_side = "right"
        model.supports_gradient_checkpointing = True
        model.language_model.supports_gradient_checkpointing = True
    elif config.model_type == "llama":
        is_vision = False
        model = LlamaForCausalLM.from_pretrained(
            train_config.model_name,
            quantization_config=bnb_config,
            use_cache=use_cache,
            attn_implementation="sdpa" if train_config.use_fast_kernels else None,
            device_map=(
                "auto"
                if train_config.quantization and not train_config.enable_fsdp
                else None
            ),
            torch_dtype=torch.float16 if train_config.use_fp16 else torch.bfloat16,
        )
    else:
        raise ValueError(
            f"Model type {config.model_type} is not supported. Please use llama or mllama model."
        )
    # Load the tokenizer and add special tokens
    tokenizer = AutoTokenizer.from_pretrained(
        (
            train_config.model_name
            if train_config.tokenizer_name is None
            else train_config.tokenizer_name
        ),
    )
    if not tokenizer.pad_token_id:
        tokenizer.pad_token_id = tokenizer.eos_token_id

    # If there is a mismatch between tokenizer vocab size and embedding matrix,
    # throw a warning and then expand the embedding matrix
    if len(tokenizer) > model.get_input_embeddings().weight.shape[0]:
        log.warning(
            "WARNING: Resizing the embedding matrix to match the tokenizer vocab size."
        )
        model.resize_token_embeddings(len(tokenizer))

    print_model_size(model, train_config, rank if train_config.enable_fsdp else 0)

    # Convert the model to bfloat16 if fsdp and pure_bf16 is enabled
    if (
        train_config.enable_fsdp
        and fsdp_config.pure_bf16
        and not train_config.quantization
    ):
        model.to(torch.bfloat16)

    if train_config.use_peft:
        # Load the pre-trained peft model checkpoint and setup its configuration
        if train_config.from_peft_checkpoint:
            model = PeftModel.from_pretrained(
                model, train_config.from_peft_checkpoint, is_trainable=True
            )
            peft_config = model.peft_config
        # Generate the peft config and start fine-tuning from original model
        else:
            peft_config = generate_peft_config(train_config, kwargs)
            model = get_peft_model(model, peft_config)
        if wandb_run:
            wandb_run.config.update(peft_config)
        model.print_trainable_parameters()

    hsdp_device_mesh_plan = None
    if (
        fsdp_config.hsdp
        and fsdp_config.sharding_strategy == ShardingStrategy.HYBRID_SHARD
    ):
        hsdp_device_mesh_plan = hsdp_device_mesh(
            replica_group_size=fsdp_config.replica_group_size,
            sharding_group_size=fsdp_config.sharding_group_size,
        )
        log.info("HSDP device mesh is ready")

    # setting up FSDP if enable_fsdp is enabled
    if train_config.enable_fsdp:
        check_fsdp_config(fsdp_config)

        if not train_config.use_peft and train_config.freeze_layers:
            freeze_transformer_layers(model, train_config.num_freeze_layers)

        mixed_precision_policy, wrapping_policy = get_policies(fsdp_config, rank)
        # Create the FSDP wrapper for MllamaSelfAttentionDecoderLayer,MllamaSelfAttentionDecoderLayer,MllamaVisionEncoderLayer in vision models
        if is_vision:
            my_auto_wrapping_policy = fsdp_auto_wrap_policy(
                model,
                [
                    MllamaSelfAttentionDecoderLayer,
                    MllamaSelfAttentionDecoderLayer,
                    MllamaVisionEncoderLayer,
                ],
            )
        else:
            # Create the FSDP wrapper for LlamaDecoderLayer in text models
            my_auto_wrapping_policy = fsdp_auto_wrap_policy(model, [LlamaDecoderLayer])
        device_id = 0
        if is_xpu_available():
            device_id = torch.xpu.current_device()
        elif torch.cuda.is_available():
            device_id = torch.cuda.current_device()
        model = FSDP(
            model,
            auto_wrap_policy=(
                my_auto_wrapping_policy if train_config.use_peft else wrapping_policy
            ),
            cpu_offload=(
                CPUOffload(offload_params=True)
                if fsdp_config.fsdp_cpu_offload
                else None
            ),
            mixed_precision=(
                mixed_precision_policy if not fsdp_config.pure_bf16 else None
            ),
            sharding_strategy=fsdp_config.sharding_strategy,
            device_mesh=hsdp_device_mesh_plan,
            device_id=device_id,
            limit_all_gathers=True,
            sync_module_states=train_config.low_cpu_fsdp,
            param_init_fn=(
                (
                    lambda module: module.to_empty(
                        device=torch.device("cuda"), recurse=False
                    )
                )
                if train_config.low_cpu_fsdp and rank != 0
                else None
            ),
        )
        if fsdp_config.fsdp_activation_checkpointing:
            model.enable_input_require_grads()
            model.gradient_checkpointing_enable()
            apply_fsdp_checkpointing(model)
    elif not train_config.quantization and not train_config.enable_fsdp:
        if is_xpu_available():
            model.to("xpu:0")
        elif torch.cuda.is_available():
            model.to("cuda")
    dataset_config = generate_dataset_config(train_config, kwargs)
    dataset_processer = processor if is_vision else tokenizer

    # Load and preprocess the dataset for training and validation
    if hasattr(dataset_train.prompter, "get_frames_features"):
        get_frames_features = dataset_train.prompter.get_frames_features
        log.info("Using frames features")
    else:
        get_frames_features = None
        log.info("Not using frames features")
    dataset_train = dataset_train.process(tokenize_dialog, dataset_processer)

    dataset_train = dataset_train.filter(
        lambda x: len(x["input_ids"]) < train_config.context_length
    )
    log.info(
        f"--> Training Set Length after filtering context length {train_config.context_length // 1000}k = {len(dataset_train)}"
    )

    if not train_config.enable_fsdp or rank == 0:
        log.info(f"--> Training Set Length = {len(dataset_train)}")
    # dataset_val = dataset_val.process(dataset_processer)
    # if not train_config.enable_fsdp or rank == 0:
    #     log.info(f"--> Validation Set Length = {len(dataset_val)}")
    dataset_val = None

    if train_config.batching_strategy == "packing":
        if is_vision:
            raise ValueError("Packing is not supported for vision datasets")
        else:
            dataset_train = ConcatDataset(
                dataset_train, chunk_size=train_config.context_length
            )

    train_dl_kwargs = get_dataloader_kwargs(
        train_config, dataset_train, dataset_processer, "train"
    )
    log.info("length of dataset_train", len(dataset_train))
    custom_data_collator = get_custom_data_collator(dataset_processer, dataset_config)
    if custom_data_collator:
        log.info("custom_data_collator is used")
        train_dl_kwargs["collate_fn"] = custom_data_collator
    # Create DataLoaders for the training and validation dataset
    train_dl_kwargs["collate_fn"] = VidCollator(train_dl_kwargs["collate_fn"])
    train_dataloader = torch.utils.data.DataLoader(
        dataset_train,
        num_workers=train_config.num_workers_dataloader,
        pin_memory=True,
        **train_dl_kwargs,
    )
    log.info(f"--> Num of Training Set Batches loaded = {len(train_dataloader)}")

    eval_dataloader = None
    if train_config.run_validation:
        if train_config.batching_strategy == "packing":
            if is_vision:
                raise ValueError("Packing is not supported for vision datasets")
            else:
                dataset_val = ConcatDataset(
                    dataset_val, chunk_size=train_config.context_length
                )

        val_dl_kwargs = get_dataloader_kwargs(
            train_config, dataset_val, dataset_processer, "val"
        )
        if custom_data_collator:
            val_dl_kwargs["collate_fn"] = custom_data_collator

        eval_dataloader = torch.utils.data.DataLoader(
            dataset_val,
            num_workers=train_config.num_workers_dataloader,
            pin_memory=True,
            **val_dl_kwargs,
        )
        log.info(f"--> Num of Validation Set Batches loaded = {len(eval_dataloader)}")
        if len(eval_dataloader) == 0:
            raise ValueError(
                "The eval set size is too small for dataloader to load even one batch. Please increase the size of eval set."
            )
        else:
            log.info(
                f"--> Num of Validation Set Batches loaded = {len(eval_dataloader)}"
            )

    # Initialize the optimizer and learning rate scheduler
    if mm_projector.finetuned:
        model_parameters = list(model.parameters()) + list(mm_projector.parameters())
    else:
        # Ensure mm_projector is on the correct device before adding to optimizer
        mm_projector = mm_projector.to(model.device)
        model_parameters = list(model.parameters())
    if fsdp_config.pure_bf16 and fsdp_config.optimizer == "anyprecision":
        optimizer = AnyPrecisionAdamW(
            # model.parameters(),
            model_parameters,
            lr=train_config.lr,
            momentum_dtype=torch.bfloat16,
            variance_dtype=torch.bfloat16,
            use_kahan_summation=False,
            weight_decay=train_config.weight_decay,
        )
    else:
        optimizer = optim.AdamW(
            # model.parameters(),
            model_parameters,
            lr=train_config.lr,
            weight_decay=train_config.weight_decay,
        )
    scheduler = StepLR(optimizer, step_size=1, gamma=train_config.gamma)

    results = train_mm(
        model=model,
        mm_projector=mm_projector,
        get_frames_features=get_frames_features,
        train_dataloader=train_dataloader,
        eval_dataloader=eval_dataloader,
        tokenizer=tokenizer,
        optimizer=optimizer,
        lr_scheduler=scheduler,
        gradient_accumulation_steps=train_config.gradient_accumulation_steps,
        train_config=train_config,
        fsdp_config=fsdp_config if train_config.enable_fsdp else None,
        local_rank=local_rank if train_config.enable_fsdp else None,
        rank=rank if train_config.enable_fsdp else None,
        wandb_run=wandb_run,
    )
    if mm_projector.finetuned:
        mm_projector_ckpt_path = (
            Path(train_config.output_dir) / "multi_modal_projector.pth"
        )
        torch.save(mm_projector.to("cpu").state_dict(), mm_projector_ckpt_path)
        log.info(f"Saved mm projector to {mm_projector_ckpt_path}")

    if not train_config.enable_fsdp or rank == 0:
        [log.info(f"Key: {k}, Value: {v}") for k, v in results.items()]
        if train_config.use_wandb:
            for k, v in results.items():
                wandb_run.summary[k] = v
