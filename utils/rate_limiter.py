import random
import time


def human_delay(delay_range: tuple[float, float]) -> None:
    time.sleep(random.uniform(*delay_range))
