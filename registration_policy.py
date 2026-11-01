"""自动注册轮换的默认参数，与页面输入共同使用。"""
MAX_SOURCE_ADDRESSES = 750

ROTATION_DEFAULTS = {
    'rotation_enabled': True, 'rotation_threshold': 495, 'rotation_count': 5,
    'delete_retries': 1, 'split_per_source': 1,
    'hourly_create_limit': 5, 'quota_window_minutes': 60,
}
