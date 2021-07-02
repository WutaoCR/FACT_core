import json
import logging
import re
import sys
from json import JSONDecodeError
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import List

from common_helper_process import execute_shell_command

from analysis.PluginBase import AnalysisBasePlugin
from helperFunctions.fileSystem import get_src_dir
from objects.file import FileObject

try:
    from ..internal.decomp import decompress
except ImportError:
    sys.path.append(str(Path(__file__).parent.parent / 'internal'))
    from decomp import decompress


MAGIC_WORD = b'IKCFG_ST\037\213'
CHECKSEC_PATH = Path(get_src_dir()) / 'bin' / 'checksec'

KERNEL_WHITELIST = [
    'kernel_heap_randomization', 'gcc_stack_protector', 'gcc_stack_protector_strong',
    'gcc_structleak', 'gcc_structleak_byref', 'slab_freelist_randomization', 'cpu_sw_domain',
    'virtually_mapped_stack', 'restrict_dev_mem_access', 'restrict_io_dev_mem_access',
    'ro_kernel_data', 'ro_module_data', 'full_refcount_validation', 'hardened_usercopy',
    'fortify_source', 'restrict_dev_kmem_access', 'strict_user_copy_check',
    'random_address_space_layout', 'arm_kernmem_perms', 'arm_strict_rodata',
    'unmap_kernel_in_userspace', 'harden_branch_predictor', 'harden_el2_vector_mapping',
    'speculative_store_bypass_disable', 'emulate_privileged_access_never',
    'randomize_kernel_address', 'randomize_module_region_full'
]

GRSECURITY_WHITELIST = [
    'grsecurity_config', 'config_pax_kernexec', 'config_pax_noexec', 'config_pax_pageexec',
    'config_pax_mprotect', 'config_pax_aslr', 'config_pax_randkstack', 'config_pax_randustack',
    'config_pax_randmmap', 'config_pax_memory_sanitize', 'config_pax_memory_stackleak',
    'config_pax_memory_uderef', 'config_pax_refcount', 'config_pax_usercopy',
    'config_grkernsec_jit_harden', 'config_bpf_jit', 'config_grkernsec_rand_threadstack',
    'config_grkernsec_kmem', 'config_grkernsec_io', 'config_grkernsec_modharden',
    'config_modules', 'config_grkernsec_chroot', 'config_grkernsec_harden_ptrace',
    'config_grkernsec_randnet', 'config_grkernsec_blackhole', 'config_grkernsec_brute',
    'config_grkernsec_hidesym'
]


class AnalysisPlugin(AnalysisBasePlugin):
    NAME = 'kernel_config'
    DESCRIPTION = 'Heuristics to find plaintext and image-embedded kernel configurations (IKCONFIG=[y|m])'
    MIME_BLACKLIST = ['audio', 'filesystem', 'image', 'video']
    DEPENDENCIES = ['file_type', 'software_components']
    VERSION = '0.2'

    def __init__(self, plugin_administrator, config=None, recursive=True):
        self.config = config

        if not CHECKSEC_PATH.is_file():
            raise RuntimeError(f'checksec not found at path {CHECKSEC_PATH}. Please re-run the backend installation.')

        self.config_pattern = re.compile(r'^(CONFIG|# CONFIG)_\w+=(\d+|[ymn])$', re.MULTILINE)
        self.kernel_pattern = re.compile(r'^# Linux.* Kernel Configuration$', re.MULTILINE)

        super().__init__(plugin_administrator, config=config, recursive=recursive, plugin_path=__file__)

    def process_object(self, file_object: FileObject) -> FileObject:
        file_object.processed_analysis[self.NAME] = dict()

        if self.object_mime_is_plaintext(file_object) and self.probably_kernel_config(file_object.binary):
            self.add_kernel_config_to_analysis(file_object, file_object.binary)
        elif file_object.file_name == 'configs.ko' or self.object_is_kernel_image(file_object):
            maybe_config = self.try_object_extract_ikconfig(file_object.binary)
            if self.probably_kernel_config(maybe_config):
                self.add_kernel_config_to_analysis(file_object, maybe_config)

        file_object.processed_analysis[self.NAME]['summary'] = self._get_summary(file_object.processed_analysis[self.NAME])

        if 'kernel_config' in file_object.processed_analysis[self.NAME]:
            file_object.processed_analysis[self.NAME]['checksec'] = self.check_kernel_config(file_object.processed_analysis[self.NAME]['kernel_config'])

        return file_object

    @staticmethod
    def _get_summary(results: dict) -> List[str]:
        if 'is_kernel_config' in results and results['is_kernel_config'] is True:
            return ['Kernel Config']
        return []

    def add_kernel_config_to_analysis(self, file_object: FileObject, config_bytes: bytes):
        file_object.processed_analysis[self.NAME]['is_kernel_config'] = True
        file_object.processed_analysis[self.NAME]['kernel_config'] = config_bytes.decode()
        self.add_analysis_tag(file_object, 'IKCONFIG', 'Kernel Configuration')

    def probably_kernel_config(self, raw_data: bytes) -> bool:
        try:
            content = raw_data.decode()
        except UnicodeDecodeError:
            return False

        config_directives = self.config_pattern.findall(content)
        kernel_config_banner = self.kernel_pattern.findall(content)

        return len(kernel_config_banner) > 0 and len(config_directives) > 0

    @staticmethod
    def try_object_extract_ikconfig(raw_data: bytes) -> bytes:
        container = raw_data
        if raw_data.find(MAGIC_WORD) < 0:
            # ikconfig is encapsulated in compression container => absence of magic word
            inner = decompress(container)
            if len(inner) == 0:
                return b''
            container = inner[0]

        start_offset = container.find(MAGIC_WORD)
        if start_offset < 0:
            return b''

        maybe_configs = decompress(container[start_offset:])

        if len(maybe_configs) == 0:
            return b''

        return maybe_configs[0]

    @staticmethod
    def object_mime_is_plaintext(file_object: FileObject) -> bool:
        analysis = file_object.processed_analysis
        return 'file_type' in analysis and \
               'mime' in analysis['file_type'] and \
               analysis['file_type']['mime'] == 'text/plain'

    @staticmethod
    def object_is_kernel_image(file_object: FileObject) -> bool:
        return 'software_components' in file_object.processed_analysis and \
               'summary' in file_object.processed_analysis['software_components'] and \
               any('linux kernel' in component.lower() for component in file_object.processed_analysis['software_components']['summary'])

    @staticmethod
    def check_kernel_config(kernel_config: str) -> dict:
        try:
            with NamedTemporaryFile() as fp:
                fp.write(kernel_config.encode())
                fp.seek(0)
                command = f'{CHECKSEC_PATH} --kernel={fp.name} --output=json 2>/dev/null'
                result = json.loads(execute_shell_command(command))
                whitelist_configs(result)
                return result
        except (JSONDecodeError, KeyError):
            logging.debug('Checksec kernel analysis failed')
        return {}


def whitelist_configs(config_results: dict):
    for key in config_results['kernel'].copy():
        if key not in KERNEL_WHITELIST:
            del config_results['kernel'][key]

    for key in config_results['grsecurity'].copy():
        if key not in GRSECURITY_WHITELIST:
            del config_results['grsecurity'][key]