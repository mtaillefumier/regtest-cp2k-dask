# Introduction

This repository contains a python script for running cp2k regtests on HPC clusters. This script was originally intended for illustrating the use of the dask workload manager in the HPC context (article in preparation) but is now a standalone script.

# Running the script

This script written in python requires the dask workload manager and the additional python package `termocolor`. Both can be installed with
```shell
pip install termcolor
pip install dask
```

Running the script requires few more steps depending on the cluster environment. It should be copied in the cp2k source directory which can be obtained with
```shell
git clone https://github.com/cp2k/cp2k.git
```
Then copy the script in the new directory. The dask workload manager should be running before running this script. The first step is given by
```shell
dask scheduler --scheduler-file scheduler.json > scheduler.out 2>&1 &
dask worker --nthreads 1 --nworkers 8 --scheduler-file scheduler.json > dask-worker.out 2>&1 &
```
The first line launch the workload manager itself while the second line creates the workers, in that example 8 workers with one thread each. The script can then be executed with the command line
```shell
python ./regtests-dask.py --input-dir $PWD --executable "PATH_TO_CP@K_BINARY/cp2k.psmp \$input_file" --scheduler-file scheduler.json
```
The executable argument is a string that should reflect how cp2k is run on a given cluster. HPC clusters using slurm will generally use `srun` for this. The field `$input_file` should be present as well as the string is replaced by the input file when the script is executed. The following batch script written for summit will run the regtests on 4 nodes after submission to the queue.
```shell
#!/bin/bash

#BSUB -P xxxxxxx
#BSUB -W 1:00
#BSUB -nnodes 4
#BSUB -alloc_flags gpumps,
#BSUB -J cp2k-regtests
#BSUB -o out.%J
#BSUB -e err.%J
#BSUB -q batch


# For wider logging
export COLUMNS=132


JSRUN_COMMAND="jsrun --nrs 1 --tasks_per_rs 2 --cpu_per_rs 6 --gpu_per_rs 1 --rs_per_host 1 -d plane:7 -b packed:3 -E OMP_NUM_THREADS=2 -E NUMEXPR_MAX_THREADS=64 cp2k.psmp \$input_file"
dask scheduler --no-dashboard --no-show --scheduler-file scheduler.json > dask-scheduler.out 2>&1 &
sleep 1
dask-worker --no-dashboard --nthreads 1 --nworkers 24 --scheduler-file scheduler.json > dask-worker.out 2>&1 &
echo "Waiting for workers to start up..."
sleep 1
python regtests-dask.py --input-dir /XYZ/codes/cp2k --executable "jsrun --nrs 1 --tasks_per_rs 2 --cpu_per_rs 6 --gpu_per_rs 1 --rs_per_host 1 -d plane:7 -b packed:3 -E OMP_NUM_THREADS=2 -E NUMEXPR_MAX_THREADS=64 /gpfs/alpine2/chp129/proj-shared/codes/spack/opt/spack/linux-rhel8-power9le/gcc-12.2.0/cp2k-master-dakmepz6nj4kz2sho7seexwpyc4loqo6/bin/cp2k.psmp \$input_file" --scheduler-file scheduler.json
# Finally, kill the job.
bkill $LSB_JOBID
```
The equivalent script for a cluster using slurm is given by
```shell
#SBATCH -N 4
#SBATCH -A account001
#SBATCH -c 4

# For wider logging
export COLUMNS=132


JSRUN_COMMAND="srun -n 2 -c 4 cp2k.psmp \$input_file"
dask scheduler --no-dashboard --no-show --scheduler-file scheduler.json > dask-scheduler.out 2>&1 &
sleep 1
dask-worker --no-dashboard --nthreads 1 --nworkers 24 --scheduler-file scheduler.json > dask-worker.out 2>&1 &
echo "Waiting for workers to start up..."
sleep 1

```

