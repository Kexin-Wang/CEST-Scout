#!/usr/bin/env bash
# Convenience wrapper: always activates the CEST_Scout conda env before running.
# Usage: scripts/run_scout.sh "brain"
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

source /etc/profile.d/modules.sh
module load miniconda3/23.11.0-2
eval "$(conda shell.bash hook)"
conda activate CEST_Scout

cd "$PROJECT_DIR"
python -m cest_scout.cli "$@"
