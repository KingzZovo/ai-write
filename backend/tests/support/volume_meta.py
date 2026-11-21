"""Complete small planning contract for routing/integrity tests."""
def valid_volume_meta(count=2):
    return {
        'volume_idx': 1, 'chapter_count': count, 'title': '渡河',
        'core_conflict': '阿青必须把证据带过封锁线，守卫追查失踪信件。',
        'turning_points': ['发现桥断', '索要通行证', '识破假证', '同伴引走追兵', '用绳渡信'],
        'emotional_arc': '从戒备到互信，发现欺骗后重新协作。',
        'transition_to_next': '人质抵达南岸，追兵开始封锁码头。',
        'volume_start_state': {'location': '北岸'}, 'volume_end_state': {'location': '南岸'},
        'volume_power_resource_map': {'守卫': '控制桥头'},
        'volume_information_map': {'阿青': '知道信件收件人'},
        'volume_mechanism_limits': '断桥不能徒步通行。',
        'volume_result_strength_ladder': ['试探', '取得协助', '交付证据'],
        'foreshadow_progression': ['绳子在开场出现', '渡信时使用绳子'],
        'new_characters': [], 'departing_characters': [],
        'foreshadows': {'planted': [], 'resolved': []},
    }
