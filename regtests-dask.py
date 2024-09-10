import argparse
import re
import time
import enum
import sys
import subprocess
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional
from string import Template
from distributed import Client, as_completed
from termcolor import colored

class ReturnCodes(enum.IntEnum):
    """possible return codes for `run_cp2k`"""

    OK = 0
    FILENOTFOUND = -1
    BAD_RUN = -2
    UNKNOWN = -3
    TIMEOUT = -4

class TestType:
    """Search pattern for result values, ie. a line in the TEST_TYPES file."""

    def __init__(self, line: str):
        self.parts = line.rsplit("!", 1)
        self.pattern = self.parts[0]
        self.pattern = self.pattern.replace("[[:space:]]", r"\s")
        for c in r"|()+":
            self.pattern = self.pattern.replace(c, f"\\{c}")  # escape special chars
        try:
            self.regex = re.compile(self.pattern)
        except Exception:
            print("Bad regex: " + self.pattern)
            raise
        self.column = int(self.parts[1])

    def grep(self, output: str) -> Optional[str]:
        for line in reversed(output.split("\n")):
            match = self.regex.search(line)
            if match:
                return line.split()[self.column - 1]
        return None
        
def list_test_types(cp2k_root):
    """

    Create a list containing all regular expressions associated to all tests

    cp2k_root: root directory for cp2k
    """
    test_type = []

    # Read TEST_TYPES.
    test_types_fn = Path(cp2k_root + "/tests/TEST_TYPES")
    lines = test_types_fn.read_text(encoding="utf8").split("\n")

    # first token is the string to grep for
    # second token is the column to look at
    for l in lines[1 : int(lines[0]) + 1]:
        ll = TestType(l)
        test_type.append(ll)

    return test_type


def list_task(cp2k_root, out_dir, test_type, flags, mpiranks):
    """

    Create a list of tasks based on the TEST_DIRS and TEST_FILES files. We
    go through the entire directory and only select the input files that satisfy
    the flags. An element of the list is structured as

    [input_file, output_file, test] 

    when no ouput is needed or

    [input_file, output_file, test, error, expected value]

    when the output needs to be checked compared to some reference value

    input parameters:

    cp2k_root: cp2k root directory
    out_dir: where to put the regtests
    test_type: list containing all the regular expressions for each test
    flags: list containing the options that cp2k supports
    mpiranks: explicit no :)

    """

    # Read TEST_DIRS.
    list_task_dir = []
    test_dirs_fn = Path(cp2k_root + "/tests/TEST_DIRS")
    for line in test_dirs_fn.read_text(encoding="utf8").split("\n"):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        token = line.split()
        include_test = True
        if len(token) > 1:
            for r in token[1:]:
                if r in flags or ("mpiranks" in r and eval(r.replace("||", " or "))):
                    include_test = True
                else:
                    include_test = False

        if not include_test:
            continue

        list_task = []
        # Read TEST_FILES.
        test_files_fn = Path(cp2k_root + "/tests/" + token[0] + "/TEST_FILES")
        for line1 in test_files_fn.read_text(encoding="utf8").split("\n"):
            line1 = line1.split("#", 1)[0].strip()
            if not line1:
                continue
            token1 = line1.split()
            token2 = []
            token2.append(
                Path(
                    cp2k_root
                    + "/"
                    + out_dir
                    + "/"
                    + token[0]
                    + "/"
                    + token1[0]))
            token2.append(
                Path(
                    cp2k_root
                    + "/"
                    + out_dir
                    + "/"
                    + token[0]
                    + "/"
                    + token1[0]
                    + ".out"
                )
            )
            if int(token1[1]) != 0:
                token2.append(test_type[int(token1[1]) - 1])
                token2.append(float(token1[2]))
                token2.append(float(token1[3]))
            list_task.append(token2)
        list_task_dir.append(list_task)
    return list_task_dir


def check_task_for_error(task, duration):
    """
    Check if the tests are correct and return the final result and duration

    returns a list [boolean, str, float]

    """
    test_pass = False
    error = -1.0
    filename = str(task[0]).split("/")[-1]
    result_string = ""
    value = "-"
    status = ""
    output = "test"
    try:
        with open(task[1]) as f:
            output = f.read()
            if len(task) > 3:
                out = float(task[2].grep(output))
                orig = float(task[4])
                err = float(task[3])
                #                print(f"{out} {err} {orig}")
                if out:
                    error = (out - orig) / orig
                    if abs(error) < err:
                        test_pass = True
                        status = colored("OK", "green")
                    else:
                        status = colored("WRONG RESULT", "red")
                value = "{out:.10g}".format(out=out)
            else:
                test_pass = True
                status = colored("OK", "green")
    except:
        print(task[1])
        test_pass = False
        value = "-"
        status = colored("RUNTIME FAILED", "red")

    result_string = (
        "{filename:<60s} {error:>20s} {status:>17s} ({duration:6.2f} sec)".format(
            filename=filename, error=value, status=status, duration=duration
        )
    )
    return [test_pass, result_string, duration]


def run_cp2k(task_info, command_line, num_nodes):
    """This will invoke a subprocess to run cp2k on a given input file 

    :param frame: for which to run cp2k
    :param num_nodes: number of nodes to use per worker
    :return: the frame, the start and stop times, and the subprocess return code
    """
    import subprocess
    import sys
    import os

    JSRUN_COMMAND_STRING = Template(command_line)

    start_time = time.time()

    # input file for cp2k (absolute path)
    filename = task_info[0]
    # output file for cp2k (absolute path)
    output_filename = task_info[1]

    if not output_filename.parents[0].exists():
        output_filename.parents[0].mkdir(parents=True)

    run_subdir = filename.absolute().parents[0]
    current_dir = Path.cwd()
    os.chdir(run_subdir)
    #    print(Path.cwd())
    # We need to create a jsrun command string to run cp2k on the given frame
    command_str = JSRUN_COMMAND_STRING.substitute(
        num_nodes=num_nodes,
        input_file=" -i " + str(filename) + " -o " + str(output_filename),
    )
    #    print(command_str)
    try:
        # run cp2k on the given frame
        completed_process = subprocess.run(
            command_str,
            shell=True,
            capture_output=True,
            timeout=300,
            check=True,  # will exit with code 1, which is non-zero, so...
            stdin=subprocess.DEVNULL,  # attempt to appease stupid MPI
        )
        return_code = completed_process.returncode

    except subprocess.CalledProcessError as e:
        # Something went wrong with cp2k since it returned a non-zero
        # exit code.
        print(f"Exception occurred. Return code {e.returncode}")
        print(f"Exception cmd: {e.cmd}")

        print(e.stdout.decode("utf-8"), file=sys.stdout, flush=True)
        print(e.stderr.decode("utf-8"), file=sys.stderr, flush=True)

        print(str(e), file=sys.stderr, flush=True)

        return_code = ReturnCodes.BAD_RUN
    except subprocess.TimeoutExpired as e:
        # Something went wrong with cp2k since it returned a non-zero
        # exit code.
        print(f"Exception occurred. Return code {e.returncode}")
        print(f"Exception cmd: {e.cmd}")

        print(e.stdout.decode("utf-8"), file=sys.stdout, flush=True)
        print(e.stderr.decode("utf-8"), file=sys.stderr, flush=True)

        print(str(e), file=sys.stderr, flush=True)

        return_code = ReturnCodes.TIMEOUT

#    except Exception as e:
#        print(f"Odd exception {e} raised")
#        print(f"Exception cmd: {e.cmd}")

#        print(e.stdout.decode("utf-8"), file=sys.stdout, flush=True)
#        print(e.stderr.decode("utf-8"), file=sys.stderr, flush=True)

#        print(str(e), file=sys.stderr, flush=True)

#        return_code = ReturnCodes.BAD_RUN

    sys.stdout.flush()  # Because Summit needs nudging
    sys.stderr.flush()
    os.chdir(current_dir)
    result_string = check_task_for_error(task_info, time.time() - start_time)
    return [
        task_info,
        result_string[0],
        result_string[1],
        result_string[2],
        return_code,
    ]


def run_subdir_cp2k(task_info, command_line, num_nodes):
    task = task_info[0]
    dir_name = task[0].parents[0]
    result_task_list = []
    start = time.time()
    for task in task_info:
        result_task_list.append(run_cp2k(task, command_line, num_nodes))
    stop = time.time()
    return dir_name, result_task_list, stop - start

#def print_error_summary(error_list):
#    try:
#        with open("error_summary") as f:
#            for ts in error_list:
#                # dump the output
                

def cp2k_version(command_line):
    JSRUN_COMMAND_STRING = Template(command_line)

    command_str = JSRUN_COMMAND_STRING.substitute(num_nodes=1, input_file="--version")
    try:
        process = subprocess.run(
            command_str,
            capture_output=True,
            shell=True,
            timeout=30,
            check=True,  # will exit with code 1, which is non-zero, so...
            stdin=subprocess.DEVNULL,  # attempt to appease stupid MPI
        )
    except subprocess.CalledProcessError as e:
        # Something went wrong with cp2k since it returned a non-zero
        # exit code.
        print(f"Exception occurred. Return code {e.returncode}")
        print(f"Exception cmd: {e.cmd}")

        print(e.stdout.decode("utf-8"), file=sys.stdout, flush=True)
        print(e.stderr.decode("utf-8"), file=sys.stderr, flush=True)

        print(str(e), file=sys.stderr, flush=True)

    finally:
        version_output = process.stdout.decode("utf8", errors="replace")
        flags_line = re.search(r" cp2kflags:(.*)\n", version_output)
        if not flags_line:
            print(version_output + "\nCould not parse feature flags.")
            sys.exit(1)
        else:
            flags = flags_line.group(1).split()
        return flags, version_output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run cp2k regtests using dask as a workflow manager"
    )
    parser.add_argument("--input-dir", type=str, help="root directory for cp2k")
    parser.add_argument("--executable", type=str, help="full command line to run cp2k. Should include srun or jsrun use $num_nodes if mpi is required")
    parser.add_argument("--scheduler-file", type=str, help="Dask scheduler file")
    args = parser.parse_args()

    flags, ck_version = cp2k_version(args.executable)
    print(f"CP2K version: {ck_version}")
    print(f"CP2K flags : {flags}")
    
    datestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    work_base_dir = f"regtesting/local/psmp/TEST-local-{datestamp}"
    shutil.copytree(args.input_dir + "/tests", work_base_dir)

    test_type = list_test_types(args.input_dir)
    lt = list_task(args.input_dir, work_base_dir, test_type, flags, 1)
    num_error = 0
    num_pass = 0

    print(args.scheduler_file)

    # list containing all failing tests and their information
    error_list = []

    start_time = time.time()
    tmp = lt[0]
    print(tmp[0])
    print(Path.cwd())
    with Client(scheduler_file=args.scheduler_file) as client:
        task_futures = client.map(
            run_subdir_cp2k, lt, command_line=args.executable, num_nodes=1
        )
        completed = as_completed(task_futures)

        for finished_task in completed:
            directory, result, duration = finished_task.result()
            duration = 0.0
            dir_file = str(directory)
            print("-------------------------------------------------------------------------")
            print(f"{dir_file:<94s} (time: {duration:7.2f} s)")
            print("-------------------------------------------------------------------------")
            for st in result:
                if st[1]:
                    num_pass += 1
                else:
                    error_list.append(result)
                print(st[2])

    stop_time = time.time()
#        print_error_summary(error_list)
    print(f"Total runtime: {stop_time - start_time:7.2f} s")
    print(f"Failed tests:  {len(error_list)}")
    print(f"passed tests:  {num_pass}")
