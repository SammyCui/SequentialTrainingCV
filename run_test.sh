#!/bin/bash
#SBATCH -N 1
#SBATCH -n 1
#SBATCH --mail-user=xcui32@fordham.edu
#SBATCH --mail-type=ALL
#SBATCH --job-name=alexnet_stb


module purge 
module load gcc5 cuda10.1
module load openmpi/cuda/64 
module load pytorch-py36-cuda10.1-gcc
module load ml-pythondeps-py36-cuda10.1-gcc
python /u/erdos/students/xcui32/cnslab/model_training/run.py