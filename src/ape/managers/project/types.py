import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ethpm_types import ContractType, PackageManifest, Source
from ethpm_types.utils import compute_checksum
from yaml import safe_dump, safe_load

from ape.api import ProjectAPI
from ape.logging import logger
from ape.managers.config import CONFIG_FILE_NAME as APE_CONFIG_FILE_NAME
from ape.utils import cached_property, get_all_files_in_directory, get_relative_path, path_match


class _ProjectSources:
    # NOTE: This class is an implementation detail and excluded from the public API.
    # It helps with diff calculations between the project's cached manifest sources
    # and the current, active sources. It's used to determine what files to compile when
    # running `ape compile`.

    def __init__(
        self,
        cached_manifest: PackageManifest,
        active_sources: Sequence[Path],
        contracts_folder: Path,
        cache_folder: Path,
    ):
        self.cached_manifest = cached_manifest
        self.active_sources = active_sources
        self.contracts_folder = contracts_folder
        self.cache_folder = cache_folder

    @cached_property
    def cached_sources(self) -> Dict[str, Source]:
        return self.cached_manifest.sources or {}

    @cached_property
    def remaining_cached_contract_types(self) -> Dict[str, ContractType]:
        cached_contract_types = self.cached_manifest.contract_types or {}

        # Filter out deleted sources.
        deleted_source_ids = self.cached_sources.keys() - set(
            map(str, [get_relative_path(p, self.contracts_folder) for p in self.active_sources])
        )
        return {
            name: contract_type
            for name, contract_type in cached_contract_types.items()
            if contract_type.source_id not in deleted_source_ids
        }

    @cached_property
    def sources_needing_compilation(self) -> List[Path]:
        needs_compile = set(filter(self._check_needs_compiling, self.active_sources))

        # NOTE: Add referring path imports for each source path
        all_referenced_paths: List[Path] = []
        sources_to_check_refs = needs_compile.copy()
        while sources_to_check_refs:
            source_id = str(get_relative_path(sources_to_check_refs.pop(), self.contracts_folder))
            reference_paths = [
                s for s in self._source_reference_paths.get(source_id, []) if s.is_file()
            ]
            all_referenced_paths.extend(reference_paths)
            needs_compile.update(reference_paths)

        needs_compile.update(all_referenced_paths)
        return list(needs_compile)

    @cached_property
    def _source_reference_paths(self) -> Dict[str, List[Path]]:
        return {
            source_id: [self.contracts_folder.joinpath(Path(s)) for s in source.references or []]
            for source_id, source in self.cached_sources.items()
        }

    def _check_needs_compiling(self, source_path: Path) -> bool:
        source_id = str(get_relative_path(source_path, self.contracts_folder))

        if source_id not in self.cached_sources:
            return True  # New file added

        cached_source = self.cached_sources[source_id]
        cached_checksum = cached_source.calculate_checksum()
        source_file = self.contracts_folder / source_path

        # ethpm_types strips trailing white space and ensures
        # a newline at the end so content so `splitlines()` works.
        # We need to do the same here for to prevent the endless recompiling bug.
        text = source_file.read_text("utf8").rstrip()
        content = f"{text}\n" if text else ""

        checksum = compute_checksum(content.encode("utf8"), algorithm=cached_checksum.algorithm)
        return checksum != cached_checksum.hash  # Contents changed

    def get_source_reference_paths(self, source_id: str) -> List[Path]:
        return [s for s in self._source_reference_paths.get(source_id, []) if s.is_file()]


class BaseProject(ProjectAPI):
    @property
    def config_file(self) -> Path:
        return self.path / APE_CONFIG_FILE_NAME

    @property
    def is_valid(self) -> bool:
        if self.config_file.is_file():
            return True

        logger.debug(
            f"'{self.path.name}' is not an 'ApeProject', but attempting to process as one."
        )

        # NOTE: We always return True as a last-chance attempt because it often
        # works anyway and prevents unnecessary plugin requirements.
        return True

    @property
    def source_paths(self) -> List[Path]:
        """
        All the source files in the project.
        Excludes files with extensions that don't have a registered compiler.

        Returns:
            List[pathlib.Path]: A list of a source file paths in the project.
        """
        files: List[Path] = []

        if not self.contracts_folder.is_dir():
            return files

        compilers = self.compiler_manager.registered_compilers
        for extension in compilers:
            ext = extension.replace(".", "\\.")
            pattern = rf"[\w|-]+{ext}"
            ext_files = get_all_files_in_directory(self.contracts_folder, pattern=pattern)
            files.extend(ext_files)

        return files

    def process_config_file(self, **kwargs) -> bool:
        if self.config_file.is_file():
            # Don't override existing config file.
            return False

        # Create a temporary config file that should be cleaned up after.
        config_data = {**kwargs}
        if self.name:
            config_data["name"] = self.name
        if self.version:
            config_data["version"] = self.version

        contracts_folder = kwargs.get("contracts_folder") or self.contracts_folder

        if contracts_folder == self.path:
            # Handle projects pointed at root path.
            contracts_folder_config_item = "."
        elif isinstance(contracts_folder, Path):
            # Strip of path prefix.
            contracts_folder_config_item = os.path.relpath(contracts_folder, self.path)
        else:
            # Was given a str.
            contracts_folder_config_item = contracts_folder

        config_data["contracts_folder"] = contracts_folder_config_item
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        self.config_file.touch()
        with open(self.config_file, "w") as file:
            safe_dump(config_data, file)

        return True

    def create_manifest(
        self, file_paths: Optional[Sequence[Path]] = None, use_cache: bool = True
    ) -> PackageManifest:
        # Read the project config and migrate project-settings to Ape settings if needed.
        compile_config = self.config_manager.get_config("compile")
        self.project_manager.load_dependencies()
        source_paths: List[Path] = list(
            set(
                [p for p in self.source_paths if p in file_paths]
                if file_paths
                else [p for p in self.source_paths if not path_match(p, *compile_config.exclude)]
            )
        )

        if use_cache:
            manifest = self.manifest
        else:
            self._contracts = None
            manifest = PackageManifest()

        # Generate sources and contract types.
        project_sources = _ProjectSources(
            manifest, source_paths, self.contracts_folder, self._cache_folder
        )
        contract_types = project_sources.remaining_cached_contract_types
        compiled_contract_types = self._compile(project_sources)
        contract_types.update(compiled_contract_types)

        # NOTE: We need to prevent compilation or else we get an endless loop, because
        # compilation results in creating a manifest, which triggers compilation, etc.
        compiler_data = self.project_manager.get_compiler_data(compile_if_needed=False)

        # Apply source and contracts to manifest.
        self.update_manifest_sources(
            source_paths,
            self.contracts_folder,
            contract_types,
            name=self.name,
            version=self.version,
            compiler_data=compiler_data,
        )

        if compiled_contract_types:
            for name, contract_type in compiled_contract_types.items():
                file = self.project_manager.local_project._cache_folder / f"{name}.json"
                file.write_text(contract_type.model_dump_json())
                self._contracts = self._contracts or {}
                self._contracts[name] = contract_type

        # Is cached.
        return self.manifest

    def _compile(
        self, project_sources: _ProjectSources, use_cache: bool = True
    ) -> Dict[str, ContractType]:
        def _compile_sources(proj_srcs: _ProjectSources) -> Dict[str, ContractType]:
            contracts_folder = self.contracts_folder
            srcs_to_compile = proj_srcs.sources_needing_compilation

            # Figure out what contracts have changed and delete them from the cache
            # so they can be compiled.
            exising_contract_types = (
                (self.cached_manifest.contract_types or {})
                if self.cached_manifest is not None
                else {}
            )
            contracts_to_remove = [
                ct
                for ct in exising_contract_types.values()
                if ct.source_id and (contracts_folder / ct.source_id) in srcs_to_compile
            ]

            for contract in contracts_to_remove:
                path = self._cache_folder / f"{contract.name}.json"
                path.unlink(missing_ok=True)

            if cached_manifest := self.cached_manifest:
                source_ids_to_remove = [ct.source_id for ct in contracts_to_remove]
                filtered_contract_types = {
                    n: ct
                    for n, ct in (cached_manifest.contract_types or {}).items()
                    if ct.source_id not in source_ids_to_remove
                }

                if self._cached_manifest is None:
                    # Shouldn't happen, but type-safety's sake.
                    self._cached_manifest = PackageManifest.model_validate({})

                self._cached_manifest.contract_types = filtered_contract_types
                self._contracts = filtered_contract_types

            return self.compiler_manager.compile(srcs_to_compile)

        if self.project_manager.path.absolute() != self.path.absolute():
            # In case compiling a dependency (or anything outside the root project).
            with self.config_manager.using_project(
                self.path, contracts_folder=self.contracts_folder
            ):
                self.project_manager.load_dependencies()
                return _compile_sources(project_sources)
        else:
            # Already in project
            return _compile_sources(project_sources)


class ApeProject(BaseProject):
    """
    The default implementation of the :class:`~ape.api.projects.ProjectAPI`.
    By default, the `:class:`~ape.managers.project.ProjectManager` uses an
    ``ApeProject`` at the current-working directory.
    """


class BrownieProject(BaseProject):
    config_file_name: str = "brownie-config.yaml"

    @property
    def brownie_config_path(self) -> Path:
        return self.path / self.config_file_name

    @property
    def is_valid(self) -> bool:
        return self.brownie_config_path.is_file()

    def process_config_file(self, **kwargs) -> bool:
        # Migrate the brownie-config.yaml file to ape-config.yaml

        migrated_config_data: Dict[str, Any] = {}
        with open(self.brownie_config_path) as brownie_config_file:
            brownie_config_data = safe_load(brownie_config_file) or {}

        # Migrate dependencies
        dependencies = []
        for dependency in brownie_config_data.get("dependencies", []):
            dependency_dict = {}
            dep_parts = dependency.split("/")
            dep_name = dep_parts[0]
            if len(dep_parts) > 1:
                dependency_dict["name"] = dep_name
                if "@" in dep_parts[1]:
                    suffix_parts = dep_parts[1].split("@")
                    dependency_dict["github"] = f"{dep_name}/{suffix_parts[0]}"
                    dependency_dict["version"] = suffix_parts[1]
                else:
                    dependency_dict["github"] = dep_parts[1]

            if dependency_dict:
                dependencies.append(dependency_dict)

        if dependencies:
            migrated_config_data["dependencies"] = dependencies

        # Migrate solidity remapping
        import_remapping = []
        solidity_version = None
        if "compiler" in brownie_config_data:
            compiler_config = brownie_config_data["compiler"]
            if "solc" in compiler_config:
                solidity_config = compiler_config["solc"]
                solidity_version = solidity_config.get("version")

                available_dependencies = [d["name"] for d in dependencies]
                brownie_import_remapping = solidity_config.get("remappings", [])

                for remapping in brownie_import_remapping:
                    parts = remapping.split("=")
                    map_key = parts[0]
                    real_path = parts[1]

                    real_path_parts = real_path.split("/")
                    dependency_name = real_path_parts[0]

                    if dependency_name in available_dependencies:
                        suffix = real_path_parts[1]
                        if "@" in suffix:
                            version_id = suffix.split("@")[1]
                            key = f"{map_key}/{self.contracts_folder.stem}"
                            entry = f"{dependency_name}/{version_id}"
                            import_remapping.append(f"{key}={entry}")
                        else:
                            import_remapping.append(
                                f"{parts[0]}/{self.contracts_folder.stem}={dependency_name}"
                            )

        if import_remapping or solidity_version:
            migrated_solidity_config: Dict[str, Any] = {}

            if import_remapping:
                migrated_solidity_config["import_remapping"] = import_remapping

            if solidity_version:
                migrated_solidity_config["version"] = solidity_version

            migrated_config_data["solidity"] = migrated_solidity_config

        return super().process_config_file(**kwargs, **migrated_config_data)
