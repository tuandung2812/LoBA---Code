#!/bin/sh
#SBATCH --job-name=my_nft_job
#SBATCH --partition=<your_partition>
#SBATCH --time=72:00:00
#SBATCH --ntasks=<number_of_tasks>
#SBATCH --nodes=<number_of_nodes>
#SBATCH --cpus-per-task=<cpus_per_task>
#SBATCH --gres=gpu:<number_of_gpus>

# Load necessary modules (if applicable)
# module load <module_name>

# Set CUDA environment (modify or remove according to your setup)
# export CUDA_HOME=<path_to_cuda>

# Environment variable settings (optional, based on your requirements)
# export CUDA_LAUNCH_BLOCKING=1
# export TORCHELASTIC_ERROR_FILE=/tmp/torch-elastic-error.json
# export NCCL_ASYNC_ERROR_HANDLING=1

# Setting a dynamic master port (optional)
export MASTER_PORT=$(shuf -i 2000-65000 -n 1)

# Path to the checkpoint and output directory (modify according to your setup)
export CKPT_PATH="MBZUAI/GLaMM-GranD-Pretrained"
export OUTPUT_DIR_PATH="train_glamm_test"

# Insert path to MedSam model in vision_prtrained
# DeepSpeed command (customize the arguments as per your needs)
deepspeed --master_port $MASTER_PORT train_ft.py \
  --version $CKPT_PATH \
  --dataset_dir '../dataset/VinDr' \
  --vision_pretrained ./LISAMed/medsam.pth \
  --exp_name $OUTPUT_DIR_PATH \
  --lora_r 8 \
  --lr 3e-4 \
  --pretrained \
  --use_segm_data \
  --segm_sample_rates "3,9,9,9,1" \
  --epochs 10 \
  --steps_per_epoch 500 \
  --mask_validation
