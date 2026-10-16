#!/bin/bash
#SBATCH --job-name=cest_scout
#SBATCH --output=outputs/slurm-%j.out
#SBATCH --time=00:30:00
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G

# Usage: sbatch scripts/run.slurm "brain"
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p outputs

source /etc/profile.d/modules.sh
module load miniconda3/23.11.0-2
eval "$(conda shell.bash hook)"
conda activate CEST_Scout

python -m cest_scout.cli "$@"
