import logging, os, platform, subprocess, re, stat, shutil

from constants import (
    APPLY_PATCH_FAIL,
    APPLY_PATCH_PASS,
    INSTALL_FAIL,
    INSTALL_PASS,
    INSTALL_TIMEOUT,
    KEY_INSTANCE_ID,
    KEY_MODEL,
    MAP_REPO_TO_INSTALL,
    MAP_REPO_TO_TEST_FRAMEWORK,
    MAP_VERSION_TO_INSTALL,
    RESET_FAILED,
    TESTS_FAILED,
    TESTS_PASSED,
    TESTS_TIMEOUT,
    TESTS_ERROR,
)
from tempfile import TemporaryDirectory
from traceback import format_exc
from typing import Dict, List
from utils import (
    clone_repo,
    get_conda_env_names,
    get_opam_switch_names,
    get_docker_container_names,
    get_environment_yml,
    get_requirements,
    get_test_directives,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger_testbed = logging.getLogger("testbed_context_manager")


class ExecWrapper:
    def __init__(
        self,
        subprocess_args: Dict = None,
    ):
        if subprocess_args is None:
            self.subprocess_args = {}
        else:
            self.subprocess_args = subprocess_args

    def __call__(self, cmd, raise_error=True, **kwargs):
        try:
            combined_args = {**self.subprocess_args, **kwargs}
            output = subprocess.run(cmd, **combined_args)
            return output
        except subprocess.CalledProcessError as e:
            if raise_error:
                logger_testbed.error(f"Error: {e}")
                logger_testbed.error(f"Error stdout: {e.stdout}")
                logger_testbed.error(f"Error stderr: {e.stderr}")
                logger_testbed.error(f"Error traceback: {format_exc()}")
                raise e


class TestbedContextManager:
    def __init__(
        self,
        task_instances: List,
        log_dir: str,
        path_conda: str = None,
        path_opam: str = None,
        testbed: str = None,
        verbose: bool = False,
        timeout: int = None,
        temp_dir: str = None,
    ):
        """
        Initialize testbed context. Creates temporary directories and groups task instances
        by repo/version.

        Args:
            task_instances (list): List of task instances
            log_dir (str): Path to log directory
            path_conda (str): Path to conda installation
            path_opam (str): Path to opam installation
            testbed (str): Path to testbed directory
            verbose (bool): Whether to show logs
            timeout (int): Timeout for actions
            temp_dir (str): Path to temporary directory
        """
        logger_testbed.propagate = verbose
        self.verbose = verbose
        self.old_dir = os.getcwd()
        self.log_dir = os.path.abspath(log_dir)
        self.timeout = timeout
        self.exec = ExecWrapper(
            subprocess_args={
                "check": True,
                "shell": False,
                "capture_output": True,
                "text": True,
            }
        )

        # Create log, temp directories if they don't exist
        if not os.path.exists(self.log_dir):
            logger_testbed.info(f"[Testbed] Creating log directory {self.log_dir}")
            os.makedirs(self.log_dir, exist_ok=True)
        if temp_dir is not None and not os.path.exists(temp_dir):
            logger_testbed.info(f"[Testbed] Creating temp directory {temp_dir}")
            os.makedirs(temp_dir, exist_ok=True)
        temp_dir = os.path.abspath(temp_dir) if temp_dir is not None else None

        # Set up conda path, create in temp directory if None
        if path_conda is not None:
            self.temp_dir_conda = None
            self.path_conda = os.path.abspath(path_conda)
        else:
            self.temp_dir_conda = TemporaryDirectory(dir=temp_dir)
            self.path_conda = self.temp_dir_conda.name
        logger_testbed.info(f"[Testbed] Using conda path {self.path_conda}")

        # Set up opam path, create in temp directory if None
        if path_opam is not None:
            self.temp_dir_opam = None
            self.path_opam = os.path.abspath(path_opam)
        else:
            self.temp_dir_opam = TemporaryDirectory(dir=temp_dir)
            self.path_opam = self.temp_dir_opam.name
        logger_testbed.info(f"[Testbed] Using opam path {self.path_opam}")

        # Set up testbed path, create in temp directory if None
        if testbed is not None:
            self.temp_dir_work = None
            self.testbed = os.path.abspath(testbed)
        else:
            self.temp_dir_work = TemporaryDirectory(dir=temp_dir)
            self.testbed = self.temp_dir_work.name
        logger_testbed.info(
            f"[Testbed] Using working directory {self.testbed} for testbed"
        )

        # Sort task instances by created_at
        self.task_instances = sorted(
            task_instances, key=lambda x: x["created_at"], reverse=True
        )

        # Group repos by repo, then version
        self.task_instances_grouped = {}
        for instance in self.task_instances:
            # Create test command from framework + directives
            test_type = MAP_REPO_TO_TEST_FRAMEWORK[instance["repo"]]
            test_directives = get_test_directives(instance)
            instance["test_cmd"] = f"{test_type} {' '.join(test_directives)}"

            # Group task instances by repo, version
            repo = instance["repo"]
            version = instance["version"] if "version" in instance else None
            if repo not in self.task_instances_grouped:
                self.task_instances_grouped[repo] = {}
            if version not in self.task_instances_grouped[repo]:
                self.task_instances_grouped[repo][version] = []
            self.task_instances_grouped[repo][version].append(instance)

        # Log grouped task instances to be run
        self.setup_refs = {}
        for repo, map_version_to_instances in self.task_instances_grouped.items():
            logger_testbed.info(
                f"[Testbed] Repo {repo}: {len(map_version_to_instances)} versions"
            )

            # Determine instances to use for environment installation
            self.setup_refs[repo] = {}
            for version, instances in map_version_to_instances.items():
                logger_testbed.info(
                    f"[Testbed] \tVersion {version}: {len(instances)} instances"
                )
                self.setup_refs[repo][version] = instances[0]

        # Remove None versions, versions not in MAP_VERSION_TO_INSTALL
        self._custom_restraints()

    def __setup_conda(self, shellenv, path_activate, exec_cmd, env_list):
        # If path_conda not provided, create temporary miniconda3 installation
        is_osx_64 = False
        if platform.system() == "Darwin" and platform.machine() == "arm64":
            is_osx_64 = True

        if self.temp_dir_conda is not None:
            # Set up the paths for Miniconda
            self.path_conda = os.path.join(self.path_conda, "miniconda3")
            os.mkdir(self.path_conda)
            miniconda_sh = os.path.join(self.path_conda, "miniconda.sh")
            logger_testbed.info(
                f"No conda path provided, creating temporary install in {self.path_conda}..."
            )

            # Download Miniconda installer
            if platform.system() == "Darwin":
                cmd_line_install_link = "https://repo.anaconda.com/miniconda/Miniconda3-latest-MacOSX-x86_64.sh"
                if is_osx_64:
                    cmd_line_install_link = "https://repo.anaconda.com/miniconda/Miniconda3-latest-MacOSX-arm64.sh"
            elif platform.system() == "Linux":
                cmd_line_install_link = "https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh"
                if platform.machine() == "aarch64":
                    cmd_line_install_link = "https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-aarch64.sh"
            else:
                raise ValueError("Unknown computer platform " + platform.system())
            download_cmd = [
                "wget",
                cmd_line_install_link,
                "-O",
                miniconda_sh,
            ]
            self.exec(download_cmd)

            # Install Miniconda
            install_cmd = ["bash", miniconda_sh, "-b", "-u", "-p", self.path_conda]
            self.exec(install_cmd)
            if is_osx_64:
                condabin = os.path.join(self.path_conda, "bin", "conda")
                config_cmd = [condabin, "config", "--env", "--set", "subdir", "osx-64"]
                self.exec(config_cmd)

            # Clean up the installer
            os.remove(miniconda_sh)

        if not hasattr(self, 'has_set_up_conda'):
            self.has_set_up_conda = True
            logger_testbed.info(f"[Testbed] Using conda path {self.path_conda}")

            # Set up conda executables, get existing environments
            self.path_conda = os.path.abspath(self.path_conda)
            conda_bin_path = os.path.join(self.path_conda, "bin")
            shellenv["PATH"] = conda_bin_path + os.pathsep + shellenv["PATH"]
            self.exec.subprocess_args["env"] = shellenv

            path_activate = os.path.join(self.path_conda, "bin", "activate")
            exec_type = "mamba" if "mamba" in self.path_conda else "conda"
            exec_cmd = os.path.join(self.path_conda, "bin", exec_type)
            env_list = get_conda_env_names(exec_cmd, shellenv)
        return shellenv, path_activate, exec_cmd, env_list


    def __setup_opam(self, shellenv, opam_exec_cmd, switch_list):
        if self.temp_dir_opam is not None:
            # Set up the paths for opam
            opam_install_sh = os.path.join(self.path_opam, "opam_install.sh")
            logger_testbed.info(
                f"No opam path provided, creating temporary install in {self.path_opam}..."
            )

            # Download opam installer
            cmd_line_install_link = "https://raw.githubusercontent.com/ocaml/opam/master/shell/install.sh"
            download_cmd = [
                "wget",
                cmd_line_install_link,
                "-O",
                opam_install_sh,
            ]
            self.exec(download_cmd)

            self.path_opam_root = os.path.join(self.path_opam, ".opam")
            os.mkdir(self.path_opam_root)

            self.path_opam = os.path.join(self.path_opam, "bin")
            os.mkdir(self.path_opam)

            # Install opam
            install_cmd = ["bash", opam_install_sh, "--download-only"]
            self.exec(install_cmd)
            # run a second time to get the name of the binary
            opam_bin = self.exec(install_cmd).stdout
            opam_match = re.search(r'Found opam binary in (.*) \.\.\.', opam_bin.strip(' \n').split('\n')[0])
            if not opam_match or not opam_match.groups(): raise Exception(f"Coult not find opam binary in {opam_bin!r}")
            temp_path_opam = opam_match.groups()[0]
            shutil.copy(temp_path_opam, os.path.join(self.path_opam, 'opam'))
            self.path_opam = os.path.join(self.path_opam, 'opam')

            # mark opam as executable
            st = os.stat(self.path_opam)
            os.chmod(self.path_opam, st.st_mode | stat.S_IEXEC)
            opam_init_cmd = [
                self.path_opam,
                "init",
                "--bare",
                "--no-setup",
                "--root",
                self.path_opam_root
            ]
            self.exec(opam_init_cmd)

            # Clean up the installer
            os.remove(opam_install_sh)
        if not hasattr(self, 'has_set_up_opam'):
            self.has_set_up_opam = True
            logger_testbed.info(f"[Testbed] Using opam path {self.path_opam}")

            # Set up opam executables, get existing environments
            self.path_opam = os.path.abspath(self.path_opam)
            opam_bin_path = os.path.dirname(self.path_opam)
            shellenv["PATH"] = opam_bin_path + os.pathsep + shellenv["PATH"]
            if self.path_opam_root is not None: shellenv["OPAMROOT"] = self.path_opam_root = os.path.abspath(self.path_opam_root)
            self.exec.subprocess_args["env"] = shellenv

            opam_exec_cmd = self.path_opam
            switch_list = get_opam_switch_names(self.path_opam, shellenv)
        return shellenv, opam_exec_cmd, switch_list

    def __enter__(self):
        """
        Set up testbed (conda environments, git repositories)
        """
        shellenv = os.environ.copy()
        self.exec.subprocess_args["env"] = shellenv
        path_activate, exec_cmd, env_list = None, None, None
        opam_exec_cmd, switch_list = None, None

        # Set up docker
        self.docker_container_name = None
        container_list = None

        # Set up testbed (environment, github repo) for each repo
        for repo, version_to_setup_ref in self.setup_refs.items():
            repo_prefix = repo.replace("/", "__")

            # Run any repo-level installation commands if provided
            if repo in MAP_REPO_TO_INSTALL:
                install_cmd = MAP_REPO_TO_INSTALL[repo]
                logger_testbed.info(
                    f"[Testbed] Running custom install command for {repo}: {install_cmd}"
                )
                self.exec(install_cmd)

            # Create conda environment per version of the repo
            for version, install in MAP_VERSION_TO_INSTALL[repo].items():
                # Skip if none of the task instances are for this version
                if version not in version_to_setup_ref:
                    continue

                # Name for both environment and github repo
                env_name = f"{repo_prefix}__{version}"
                logger_testbed.info(f"[Testbed] Setting up testbed for {env_name}")

                # Clone github per repo/version
                repo_path = os.path.join(self.testbed, env_name)
                if not os.path.exists(repo_path):
                    clone_repo(repo, repo_path)
                    logger_testbed.info(f"[Testbed] Cloned {repo} to {repo_path}")
                else:
                    logger_testbed.info(
                        f"[Testbed] Repo for {repo_prefix} version {version} exists: {repo_path}; skipping"
                    )

                # Skip if conda environment already exists
                if version.startswith("coq."):
                    shellenv, opam_exec_cmd, switch_list = self.__setup_opam(shellenv, opam_exec_cmd, switch_list)
                    if env_name in switch_list:
                        logger_testbed.info(
                            f"[Testbed] Environment {env_name} already exists in opam; skipping"
                        )
                        continue
                elif version.startswith("docker-coq."):
                    self.docker_container_name = env_name
                    if container_list is None: container_list = get_docker_container_names(shellenv)
                    if env_name in container_list:
                        logger_testbed.info(
                            f"[Testbed] Environment {env_name} already exists as a docker container; removing"
                        )
                        cmd = f"docker rm {self.docker_container_name}"
                        logger_testbed.info(
                            f"[Testbed] Removing docker container {env_name}; Command: {cmd}"
                        )
                        self.exec(cmd.split(" "))
                else:
                    shellenv, path_activate, exec_cmd, env_list = self.__setup_conda(shellenv, path_activate, exec_cmd, env_list)
                    if env_name in env_list:
                        logger_testbed.info(
                            f"[Testbed] Environment {env_name} already exists; skipping"
                        )
                        continue

                # Get setup reference instance
                setup_ref_instance = version_to_setup_ref[version]

                # Create conda environment according to install instructinos
                pkgs = install["packages"] if "packages" in install else ""
                if pkgs == "requirements.txt":
                    # Create environment
                    cmd = (
                        f"{exec_cmd} create -n {env_name} python={install['python']} -y"
                    )
                    logger_testbed.info(
                        f"[Testbed] Creating environment {env_name}; Command: {cmd}"
                    )
                    self.exec(cmd.split(" "))

                    # Install dependencies
                    path_to_reqs = get_requirements(setup_ref_instance, self.testbed)
                    cmd = f". {path_activate} {env_name} && echo 'activate successful' && pip install -r {path_to_reqs}"
                    logger_testbed.info(
                        f"[Testbed] Installing dependencies for {env_name}; Command: {cmd}"
                    )
                    self.exec(cmd, shell=True)
                    os.remove(path_to_reqs)
                elif pkgs == "environment.yml":
                    # Create environment from yml
                    path_to_reqs = get_environment_yml(
                        setup_ref_instance, env_name, self.testbed
                    )
                    if "no_use_env" in install and install["no_use_env"]:
                        # `conda create` based installation
                        cmd = f"{exec_cmd} create -c conda-forge -n {env_name} python={install['python']} -y"
                        logger_testbed.info(
                            f"[Testbed] Creating environment {env_name}; Command: {cmd}"
                        )
                        self.exec(cmd.split(" "))

                        # Install dependencies
                        cmd = f"{exec_cmd} env update -f {path_to_reqs}"
                        logger_testbed.info(
                            f"[Testbed] Installing dependencies for {env_name}; Command: {cmd}"
                        )
                        self.exec(cmd.split(" "))
                    else:
                        # `conda env create` based installation
                        cmd = f"{exec_cmd} env create --file {path_to_reqs}"
                        logger_testbed.info(
                            f"[Testbed] Creating environment {env_name}; Command: {cmd}"
                        )
                        self.exec(cmd.split(" "))

                    # Remove environment.yml
                    os.remove(path_to_reqs)
                elif version.startswith("coq."):
                    # `opam` based installation
                    root_arg = f"--root={self.path_opam_root}" if self.path_opam_root else ""
                    cmd = f"{opam_exec_cmd} switch create -y {root_arg} {env_name} {install['ocaml']}"
                    logger_testbed.info(
                        f"[Testbed] Creating opam switch {env_name}; Command: {cmd}"
                    )
                    self.exec(cmd.split(" "))

                    if install["packages"]:
                        cmd = f"{opam_exec_cmd} install -y {root_arg} --switch={env_name} {install['packages']}"
                        logger_testbed.info(
                            f"[Testbed] Installing packages for switch {env_name}; Command: {cmd}"
                        )
                        self.exec(cmd.split(" "))

                    if install["deps_only_packages"]:
                        cmd = f"{opam_exec_cmd} install -y --deps-only {root_arg} --switch={env_name} {install['deps_only_packages']}"
                        logger_testbed.info(
                            f"[Testbed] Installing deps-only packages for switch {env_name}; Command: {cmd}"
                        )
                        self.exec(cmd.split(" "))
                elif version.startswith("docker-coq."):
                    self.docker_container_name = env_name
                    cmd = f"docker run -d --name {self.docker_container_name} coqorg/coq:{version[len('docker-coq.'):]} -v {repo_path}:/home/coq/workdir"
                    logger_testbed.info(
                        f"[Testbed] Creating docker container {env_name}; Command: {cmd}"
                    )
                    self.exec(cmd.split(" "))
                else:
                    # Create environment + install dependencies
                    cmd = f"{exec_cmd} create -n {env_name} python={install['python']} {pkgs} -y"
                    logger_testbed.info(
                        f"[Testbed] Creating environment {env_name}; Command: {cmd}"
                    )
                    self.exec(cmd.split(" "))

                # Install additional packages if specified
                if "pip_packages" in install:
                    cmd = f". {path_activate} {env_name} && pip install {install['pip_packages']}"
                    logger_testbed.info(
                        f"[Testbed] Installing pip packages for {env_name}; Command: {cmd}"
                    )
                    self.exec(cmd.split(" "))

        return self

    def get_distributed_tasks(self) -> List:
        """
        Create task group (instances + keywords) for each repo/version

        Returns:
            list: List of task groups, each group containing task instances
                from the same repo with the same version
        """
        distributed_tasks = []
        for repo, map_version_to_instances in self.task_instances_grouped.items():
            repo_prefix = repo.replace("/", "__")
            for version, instances in map_version_to_instances.items():
                env_name = f"{repo_prefix}__{version}"
                task_set = {
                    "conda_path": self.path_conda,
                    "opam_path": self.path_opam,
                    "opam_root": self.path_opam_root,
                    "log_dir": self.log_dir,
                    "task_instances": instances,
                    "testbed": os.path.join(self.testbed, env_name),
                    "timeout": self.timeout,
                    "venv": env_name,
                    "switch": env_name,
                    "container": env_name,
                    "version": version,
                    "verbose": self.verbose,
                }
                distributed_tasks.append(task_set)
        return distributed_tasks

    def _custom_restraints(self):
        """
        Custom restraints per repo
        """
        for repo, group in self.task_instances_grouped.items():
            if None in group:
                logger_testbed.info(f"[Testbed] Removed None version from repo {repo}")
                del group[None]
            versions = list(group.keys())
            for version in versions:
                if version not in MAP_VERSION_TO_INSTALL[repo]:
                    logger_testbed.info(
                        f"[Testbed] Removed {version} version from repo {repo} (Install instructions not given)"
                    )
                    del group[version]

    def __exit__(self, exc_type, exc_value, exc_traceback):
        if self.temp_dir_work is not None:
            self.temp_dir_work.cleanup()
        if self.temp_dir_conda is not None:
            self.temp_dir_conda.cleanup()
        if self.docker_container_name is not None:
            for action, description in (("stop", "Stopping"), ("rm", "Deleting")):
                cmd = f"docker {action} {self.docker_container_name}"
                logger_testbed.info(
                    f"[Testbed] {description} docker container {self.docker_container_name}; Command: {cmd}"
                )
                self.exec(cmd.split(" "))

logger_taskenv = logging.getLogger("taskenv_context_manager")


class TaskEnvContextManager:
    def __init__(
        self,
        instance: Dict,
        testbed: str,
        venv: str,
        switch: str,
        container: str,
        log_dir: str,
        conda_path: str,
        opam_path: str,
        opam_root: str,
        verbose: bool = False,
        timeout: int = None,
        is_eval: bool = False,
    ):
        """
        Sets up execution context for a single task instance

        Args:
            instance (dict): Task instance
            testbed (str): Path to testbed directory
            venv (str): Name of conda environment (should exist in conda_path)
            switch (str): Name of opam switch (should exist in opam switches)
            container (str): Name of docker container (should exist in docker ps -a)
            log_dir (str): Path to log directory
            conda_path (str): Path to conda installation
            opam_path (str): Path to opam installation
            opam_root (str): Path to .opam root of opam installation
            verbose (bool): Whether to show logs
            timeout (int): Timeout for actions
            is_eval (bool): Whether this is for evaluating a model on SWE Bench
                (Mainly for logging purposes)
        """
        logger_taskenv.propagate = verbose
        self.instance = instance
        self.testbed = testbed
        self.testbed_name = testbed.split("/")[-1]
        self.venv = venv
        self.switch = switch
        self.container = container
        self.conda_path = conda_path
        self.opam_path = opam_path
        self.opam_root = opam_root
        self.log_file = os.path.join(log_dir, f"{instance[KEY_INSTANCE_ID]}.log")
        self.is_eval = is_eval
        if is_eval:
            self.log_file = os.path.join(
                log_dir, f"{instance[KEY_INSTANCE_ID]}.{instance[KEY_MODEL]}.eval.log"
            )
        if self.instance["version"].startswith("coq."):
            root_arg = f"--root={self.opam_root}" if self.opam_root else ""
            cmd_activate = f"{self.opam_path} switch {root_arg} {self.switch} && eval $({self.opam_path} env) && echo 'activate successful'"
            self.cmd_activate_then = lambda cmd: f"{cmd_activate} && {cmd}"
        elif self.instance["version"].startswith("docker-coq."):
            self.cmd_activate_then = lambda cmd: f"docker run -v .:/home/coq/workdir -it coqorg/coq:{self.instance['version'][len('docker-coq.'):]} {cmd}"
        else:
            cmd_activate = f". {os.path.join(self.conda_path, 'bin', 'activate')} {self.venv} && echo 'activate successful'"
            self.cmd_activate_then = lambda cmd: f"{cmd_activate} && {cmd}"
        self.timeout = timeout
        self.cwd = os.getcwd()

        shellenv = os.environ.copy()
        condabinpath = os.path.join(self.conda_path, "bin")
        opambinpath = os.path.dirname(self.opam_path)
        shellenv["PATH"] = condabinpath + os.pathsep + opambinpath + os.pathsep + shellenv["PATH"]
        self.exec = ExecWrapper(
            subprocess_args={
                "check": True,
                "shell": False,
                "capture_output": True,
                "text": True,
                "env": shellenv,
            }
        )

    def __enter__(self):
        """
        Enter task environment, set up log file
        """
        os.chdir(self.testbed)
        with open(self.log_file, "w") as f:
            f.write(
                f"Task Metadata:\n\t- Instance ID: {self.instance[KEY_INSTANCE_ID]}\n\t- Testbed: {self.testbed}\n\t- Virtual Env.: {self.venv}\n"
            )
            if self.is_eval:
                f.write(f"\t- Evaluation Model: {self.instance[KEY_MODEL]}\n")
        return self

    def reset_task_env(self, instance: Dict):
        """
        Reset task environment + testbed and checkout base commit of given task instance

        Args:
            instance (dict): Task instance
        Returns:
            bool: True if reset successful, False otherwise
        """
        try:
            # Remove all paths in .gitignore
            if os.path.exists(".gitignore"):
                self.exec(
                    "git ls-files --ignored --exclude-standard -o -z | xargs -0 -r rm -rf".split(),
                    raise_error=False,
                )

            # Reset git repo + checkout base commit
            self.exec("git restore .".split(" "))
            self.exec("git reset HEAD .".split(" "))
            self.exec("git clean -fdx".split(" "))
            self.exec("git submodule foreach --recursive git clean -fdx".split(" "))
            self.exec(
                f"git -c advice.detachedHead=false checkout --recurse-submodules -f {instance['base_commit']}".split(
                    " "
                )
            )
            self.exec("git submodule update --init --recursive".split(" "))
            logger_taskenv.info(
                f"[{self.testbed_name}] [{instance[KEY_INSTANCE_ID]}] Reset task environment to {instance['base_commit']}"
            )
            return True
        except Exception as e:
            err_msg = f"{RESET_FAILED}; Failed to reset task environment to {instance['base_commit']}: {e}"
            logger_taskenv.error(f"[{self.testbed_name}] {err_msg}")
            with open(self.log_file, "a") as f:
                f.write(err_msg)
            return False

    def run_install_task(self, instance: Dict) -> bool:
        """
        Run installation for task instance

        Args:
            instance (dict): Task instance
        Returns:
            bool: True if installation successful, False otherwise
        """
        # Get installation instructions by repo/version
        specifications = MAP_VERSION_TO_INSTALL[instance["repo"]][instance["version"]]

        # Run pre-install set up if provided
        if "pre_install" in specifications:
            for pre_install in specifications["pre_install"]:
                cmd_pre_install = self.cmd_activate_then(pre_install)
                logger_taskenv.info(
                    f"[{self.testbed_name}] [{instance[KEY_INSTANCE_ID]}] Running pre-install setup command: {cmd_pre_install}"
                )
                out_pre_install = self.exec(
                    cmd_pre_install, timeout=self.timeout, shell=True
                )
                with open(self.log_file, "a") as f:
                    f.write(f"Pre-installation Command: {cmd_pre_install}\n")
                    f.write(f"Std. Output: {out_pre_install.stdout}\n")
                    f.write(f"Std. Error: {out_pre_install.stderr}\n")
                if out_pre_install.returncode != 0:
                    logger_taskenv.error(
                        f"[{self.testbed_name}] [{instance[KEY_INSTANCE_ID]}] Pre-install setup failed"
                    )
                    with open(self.log_file, "a") as f:
                        f.write(f"\n{INSTALL_FAIL}\n")
                    return False

        # Skip installation if no instructions provided
        if "install" not in specifications:
            return True

        cmd_install = self.cmd_activate_then(specifications["install"])
        logger_taskenv.info(
            f"[{self.testbed_name}] [{instance[KEY_INSTANCE_ID]}] Installing with command: {cmd_install}"
        )
        try:
            # Run installation command
            out_install = self.exec(cmd_install, timeout=self.timeout, shell=True)

            # Write installation logs to log file
            with open(self.log_file, "a") as f:
                f.write(f"Installation Command: {cmd_install}\n")
                f.write(f"Std. Output: {out_install.stdout}\n")
                f.write(f"Std. Error: {out_install.stderr}\n")

            if out_install.returncode != 0:
                # Installation failed
                logger_taskenv.error(
                    f"[{self.testbed_name}] [{instance[KEY_INSTANCE_ID]}] Installation failed"
                )
                with open(self.log_file, "a") as f:
                    f.write(f"\n{INSTALL_FAIL}\n")
                return False

            # Installation successful
            logger_taskenv.info(
                f"[{self.testbed_name}] [{instance[KEY_INSTANCE_ID]}] Installation successful"
            )
            with open(self.log_file, "a") as f:
                f.write(f"\n{INSTALL_PASS}\n")
            return True
        except subprocess.TimeoutExpired:
            # Installation timed out
            logger_taskenv.error(
                f"[{self.testbed_name}] [{self.instance[KEY_INSTANCE_ID]}] Installation timed out"
            )
            with open(self.log_file, "a") as f:
                f.write(f"\n{INSTALL_TIMEOUT}\n")
            return False

    def apply_patch(
        self, patch: str, patch_type: str = "", revert: bool = False
    ) -> bool:
        """
        Apply patch to task environment

        Args:
            patch (str): Plaintext of patch to apply
            patch_type (str): Type of patch (e.g. "eval", "test")
        Returns:
            bool: True if patch applied successfully, False otherwise
        """
        # If patch is `None`, indicate in log and skip
        if patch is None:
            logger_taskenv.error(
                f"[{self.testbed_name}] [{self.instance[KEY_INSTANCE_ID]}] Patch is `None` ({patch_type})"
            )
            with open(self.log_file, "a") as f:
                f.write(f"{APPLY_PATCH_FAIL}; Prediction patch is `None`")
            return False

        # Write patch to temporary patch file in parent directory
        patch_path = os.path.join(
            os.path.dirname(self.testbed.rstrip("/")),
            f"temp_{self.instance[KEY_INSTANCE_ID]}_{patch_type}.patch",
        )
        with open(patch_path, "w") as f:
            f.write(patch)

        # Apply patch to testbed directory
        apply_cmd = (
            f"git apply -v -R {patch_path}" if revert else f"git apply -v {patch_path}"
        )
        out_patch = self.exec(apply_cmd.split(" "), raise_error=False, check=False)
        os.remove(patch_path)

        log_cmd = "Revert" if revert else "Apply"
        if out_patch.returncode != 0:
            # Patch apply failed
            logger_taskenv.error(
                f"[{self.testbed_name}] [{self.instance[KEY_INSTANCE_ID]}] {log_cmd} patch failed ({patch_type})"
            )
            with open(self.log_file, "a") as f:
                f.write(f"{APPLY_PATCH_FAIL}; ({patch_type})\nOutput:\n")
                f.write(out_patch.stdout)
                f.write(out_patch.stderr)
            return False

        # Patch apply succeeded
        logger_taskenv.info(
            f"[{self.testbed_name}] [{self.instance[KEY_INSTANCE_ID]}] {log_cmd} patch successful ({patch_type})"
        )
        with open(self.log_file, "a") as f:
            f.write(f"{APPLY_PATCH_PASS} ({patch_type})\n")
        return True

    def run_tests_task(self, instance: Dict):
        """
        Run tests for task instance

        Args:
            instance (dict): Task instance
        Returns:
            bool: True if test script ran successfully, False otherwise
        """
        try:
            # Run test command for task instance
            test_cmd = self.cmd_activate_then(instance["test_cmd"])
            with open(self.log_file, "a") as f:
                f.write(f"Test Script: {test_cmd};\n")
            out_test = self.exec(test_cmd, shell=True, timeout=self.timeout, check=False)

            # Write test results to log file
            with open(self.log_file, "a") as f:
                f.write(f"Output:\n")
                f.write(out_test.stdout)
                f.write(out_test.stderr)
                if out_test.returncode != 0:
                    f.write(f"\n{TESTS_FAILED}\n")
                else:
                    f.write(f"\n{TESTS_PASSED}\n")

            logger_taskenv.info(
                f"[{self.testbed_name}] [{instance[KEY_INSTANCE_ID]}] Test script run successful"
            )
            return True
        except subprocess.TimeoutExpired:
            # Test command run timed out
            logger_taskenv.error(
                f"[{self.testbed_name}] [{instance[KEY_INSTANCE_ID]}] Test script run time out {self.timeout}"
            )
            with open(self.log_file, "a") as f:
                f.write(f"{TESTS_TIMEOUT} after {self.timeout} seconds\n")
            return False
        except Exception as e:
            # Test command run failed
            logger_taskenv.error(
                f"[{self.testbed_name}] [{instance[KEY_INSTANCE_ID]}] Test script run failed"
            )
            with open(self.log_file, "a") as f:
                f.write(f"{TESTS_ERROR}: {e}")
            return False

    def __exit__(self, exc_type, exc_value, exc_traceback):
        os.chdir(self.cwd)
