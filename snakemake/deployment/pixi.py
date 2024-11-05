from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from typing import Optional, Union
from snakemake.sourcecache import (
    LocalGitFile,
    LocalSourceFile,
    SourceFile,
    infer_source_file,
)
import subprocess
import tempfile
import hashlib
import shutil
import json
from glob import glob
import tarfile
import zipfile
import uuid
from enum import Enum
import threading
import shutil
from abc import ABC, abstractmethod

from snakemake.exceptions import CreateCondaEnvironmentException, WorkflowError
from snakemake.logging import logger
from snakemake.common import (
    is_local_file,
    parse_uri,
    ON_WINDOWS,
)
from snakemake.deployment import singularity, containerize
from snakemake.io import (
    IOFile,
    apply_wildcards,
    contains_wildcard,
    _IOFile,
)
from snakemake_interface_common.utils import lazy_property

import asyncio

from rattler import LockFile, Platform, solve, install, Environment
from rattler.shell import Shell, activate, ActivationVariables

from snakemake.workflow import Workflow

# a general exception for any error that occurs during pixi
class PixiError(WorkflowError):
    pass

@dataclass
class PixiEnv:
    lockfile: _IOFile
    env_name: str
    container_img: Optional[str]
    workflow: Workflow
    envs_directory: Path = Path(".snakemake/pixi")
    default_shell: Optional[Shell] = Shell.bash
    rattler_env: Environment = field(init=False)

    def __post_init__(self) -> None:
        self.lockfile.check()
        lockfile_path = Path(str(self.lockfile.file))
        assert lockfile_path.exists()
        if (rattler_lock := LockFile.from_path(lockfile_path)) is None:
            raise PixiError(
                f"Pixi lockfile {lockfile_path} is not a valid lockfile."
            )
        
        if (rattler_env := rattler_lock.environment(name=self.env_name)) is None:
            raise PixiError(
                f"Pixi environment {self.env_name} not found in lockfile {lockfile_path}."
                f"Available environments: {[name for name,env in rattler_lock.environments()]}" 
            )
        self.rattler_env = rattler_env
        assert Platform.current() in self.platforms

    def create(self, dryrun=False):
        """Create the conda environment using rattler and the lockfile."""
        logger.info(
            f"Creating pixi environment {self.env_name} from lockfile {self.lockfile.file}"
        )

        # Create the directory for the environment if it doesn't exist
        self.envs_directory.mkdir(parents=True, exist_ok=True)

        # Create the environment directory
        env_dir = self.envs_directory / self.env_name
        env_dir.mkdir(parents=True, exist_ok=True)
        asyncio.run(
          install()
        )
        # result = asyncio.run(
        #     activate(
        #       prefix=env_dir,
        #       shell=self.default_shell,
        #       activation_variables=ActivationVariables(activate=True),
        #     )
        # )

    @property
    def platforms(self):
        return self.rattler_env.platforms()

    @property
    def channels(self):
        return self.rattler_env.channels()

    @property
    def records(self):
        return self.rattler_env.conda_repodata_records_for_platform(Platform.current())

    @property
    def dependencies(self):
        if pkgs := self.rattler_env.packages(Platform.current()):
          return [pkg.name for pkg in pkgs]
        raise PixiError("PixiEnv.dependencies: No dependencies found.")

    def __repr__(self):
        return f"PixiEnv(lockfile={self.lockfile}, env_name={self.env_name}, container_img={self.container_img})"


class PixiEnvSpec(ABC):
    @abstractmethod
    def get_pixi_env(self, workflow, container_img=None, cleanup=None): ...

    @abstractmethod
    def check(self): ...

    @property
    def is_file(self): ...

    @abstractmethod
    def __hash__(self): ...

    @abstractmethod
    def __eq__(self, other): ...


class PixiEnvLockfileSpec(PixiEnvSpec):
    lockfile: _IOFile
    lockfile_path: Path
    env_name: str

    def __init__(self, lockfile, name: str, rule=None):
        logger.debug(f"PixiEnvLockfileSpec: {lockfile=}, {name=}, {rule=}")
        self.lockfile_path = Path(lockfile)
        logger.debug(f"PixiEnvLockfileSpec: self.lockfilepath {self.lockfile_path=}")
        assert self.lockfile_path.exists()
        if isinstance(lockfile, SourceFile):
            self.lockfile = IOFile(str(lockfile.get_path_or_uri()), rule=rule)
        elif isinstance(lockfile, _IOFile):
            self.lockfile = lockfile
        else:
            self.lockfile = IOFile(lockfile, rule=rule)
        self.env_name = name

    def check(self):
        self.lockfile.check()

    def get_pixi_env(self, workflow, container_img=None, cleanup=None):
        # raise NotImplementedError("PixiEnvLockfileSpec.get_pixi_env")
        return PixiEnv(
            workflow=workflow,
            lockfile=self.lockfile,
            env_name=self.env_name,
            container_img=container_img,
        )

    @property
    def is_file(self):
        return True

    def __hash__(self):
        """A unique hash for the instance of PixiEnvLockfileSpec"""
        return hash((self.lockfile, self.env_name))

    def __eq__(self, other):
        return self.lockfile == other.lockfile
