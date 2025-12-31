import re
from . import loggin_utils
'''
def parse_folder_name_from_bucket(p_bucket):
    parts = p_bucket.split('-')
    prefix_code = parts[0] if len(parts) >= 1 else 'UNK'
    code = parts[4] if len(parts) >= 5 else 'XXX'
    return f"{prefix_code}-EEFF-GG-{code}"
'''
def parse_folder_name_from_bucket(p_bucket):
    parts = p_bucket.split('-')
    prefix_code = parts[0] if len(parts) >= 1 else 'UNK'
    code = parts[4] if len(parts) >= 5 else 'XXX'
    folder_name = f"{prefix_code}-EEFF-GG-{code}"
    loggin_utils.log(f"Parsed folder name: {folder_name} from pBucket: {p_bucket}")
    return folder_name

def extract_size_from_bucket_parts(parts) -> str | None:
    """
    parts[2] looks like '25MT' or similar. Return the leading digits (e.g. '25').
    """
    if not parts or len(parts) < 3:
        return None
    m = re.match(r'(\d+)', str(parts[2]))
    return m.group(1) if m else None

def build_b1_config_name_from_parts(parts) -> str:
    """
    KHF-B1PM-25-PT-FLT-2   (no MT, last segment not zero-padded)
    Uses parts[0], size (from parts[2]), parts[3], parts[4], parts[5]
    """
    if len(parts) < 6:
        raise ValueError(f"parts not in expected shape: {parts}")
    size = extract_size_from_bucket_parts(parts) or ""
    suffix = str(parts[5]).lstrip('#')
    name = f"{parts[0]}-B1PM-{size}-{parts[3]}-{parts[4]}-{suffix}"
    loggin_utils.log(f"[path_utils] B1 target: {name}")
    return name

def build_b2_config_name_from_parts(parts) -> str:
    """
    KHF-B2PM-25MT-PT-FLT-2 (exact bucket style)
    Uses parts[0], parts[2] (with MT), parts[3], parts[4], parts[5]
    """
    if len(parts) < 6:
        raise ValueError(f"parts not in expected shape: {parts}")
    suffix = str(parts[5]).lstrip('#')
    name = f"{parts[0]}-B2PM-{parts[2]}-{parts[3]}-{parts[4]}-{suffix}"
    loggin_utils.log(f"[path_utils] B2 target: {name}")
    return name

def strip_delta_suffix(config_name):
    parts = config_name.split('-')
    if parts and re.fullmatch(r'\d{3,4}', parts[-1]):
        parts = parts[:-1]
    if parts and re.fullmatch(r'\d{3,4}', parts[-1]):
        parts = parts[:-1]
    return '-'.join(parts)