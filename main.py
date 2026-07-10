"""
Code for tomato watering system.

Pots have copper tape on opposing sides creating a capacitor. Capacitance
changes when the soil is wet since the permittivity of water is much
higher than that of soil. Capacitance also decreases when temperature
increases (permittivity of warm water is lower than cold water), and
evaporative cooling further complicates measuring an absolute degree of
"wetness" - we can nonetheless clearly identify "recently watered" and
"dry."

See REQUIREMENTS.md for the pin map, current requirements, and planned
work.
"""

from machine import Pin, Timer, disable_irq, enable_irq
import time

LOG_FILE_1 = "cap_log.csv"
LOG_FILE_2 = "cap_log_2.csv"
PUMP_LOG_FILE = "pump_log.csv"
INTERVAL_SECONDS = 5 * 60
TIMEOUT_US = 1000
NUM_READINGS = 20
LONG_PRESS_MS = 1000  # minimum hold time to register as a start/stop press
MAX_PUMP_RUN_MS = 3 * 60 * 1000  # hard cap on any single watering activation
IDLE_POLL_MS = 100  # how often the main loop checks whether watering has finished

# Automatic threshold-based watering: each pot gets one MAX_PUMP_RUN_MS pulse
# whenever its hourly mean charge time drops below its threshold, subject to
# a cooldown and a daily cap. See REQUIREMENTS.md "Automatic watering".
THRESHOLDS_US = {1: 225, 2: 255}
SAMPLES_PER_HOUR = 3600 // INTERVAL_SECONDS  # 12
COOLDOWN_SECONDS = SAMPLES_PER_HOUR * INTERVAL_SECONDS  # 3600s
DAILY_CAP = 8
ROLLING_WINDOW_SECONDS = 24 * 60 * 60

SENSOR_LOG_HEADER = "seconds_since_start,mean_us,min_us,max_us\n"
PUMP_LOG_HEADER = "seconds_since_start,pump,pulse_count\n"

sensor1_charge = Pin(18, Pin.OUT)
sensor1_sense = Pin(19, Pin.OUT)
sensor2_charge = Pin(21, Pin.OUT)
sensor2_sense = Pin(22, Pin.OUT)

pump1 = Pin(17, Pin.OUT)
pump2 = Pin(20, Pin.OUT)
pumps = {1: pump1, 2: pump2}

# Each button shorts one GPIO straight to GND when pressed, read here with
# an internal pull-up. GP9 and GP3 (button 1/2's former second leg) are no
# longer read in firmware - disconnect them physically when convenient.
button1_pin = Pin(10, Pin.IN, Pin.PULL_UP)
button2_pin = Pin(5, Pin.IN, Pin.PULL_UP)
button_pins = {1: button1_pin, 2: button2_pin}

green_led1 = Pin(8, Pin.OUT)
yellow_led1 = Pin(6, Pin.OUT)
green_led2 = Pin(2, Pin.OUT)
yellow_led2 = Pin(1, Pin.OUT)
red_led = Pin(0, Pin.OUT)
all_leds = [green_led1, yellow_led1, green_led2, yellow_led2, red_led]

pump_timers = {1: Timer(-1), 2: Timer(-1)}
active_pump = None
press_start_ms = {1: 0, 2: 0}

SENSOR_LOG_FILES = {1: LOG_FILE_1, 2: LOG_FILE_2}
hourly_buffers = {1: [], 2: []}
last_pulse_time = {1: None, 2: None}  # None = not pulsed yet this boot
pulse_history = {1: [], 2: []}  # rolling 24h list of this pot's auto-pulse timestamps

# Both pots' hourly buffers always fill in lockstep (see the main loop), so
# when both need water in the same hour, pot 1 is evaluated first and holds
# the mutual-exclusion lock for its whole run. pending_watering lets pot 2's
# blocked trigger fire as soon as pot 1's pump stops, instead of waiting a
# full extra hour for its own next evaluation - the main loop services it,
# not stop_pump itself (see the main loop for why).
pending_watering = {1: None, 2: None}  # None, or the hourly mean_us that was blocked


def ensure_log_header(log_file, header):
    try:
        with open(log_file, "r") as f:
            pass
    except OSError:
        with open(log_file, "w") as f:
            f.write(header)


def read_cap_time_us(charge, sense):
    # ladda ur
    # Redefiniera sense pin:
    sense.init(Pin.OUT)
    time.sleep_ms(20)

    charge.value(0)
    sense.value(0)
    time.sleep_ms(10)

    # starta laddning
    # Gör sense till högimpediv ingång
    sense.init(Pin.IN)
    # Liten paus så pinläget hinner stabiliseras
    time.sleep_us(20)

    start = time.ticks_us()
    charge.value(1)

    while sense.value() == 0:
        if time.ticks_diff(time.ticks_us(), start) > TIMEOUT_US:
            break
    # Set charge value to "no current."
    charge.value(0)
    return time.ticks_diff(time.ticks_us(), start)


def median(values):
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2


def sample_sensor_median(charge, sense):
    """Take NUM_READINGS readings and return their median in microseconds.

    Aborts early (discarding the partial batch) if a pump activates mid-read,
    since watering takes priority over sensor reading. Returns None if
    aborted. Raw readings are never written to flash - only the hourly
    aggregate that record_hourly_sample later derives from them.
    """
    readings = []
    for i in range(NUM_READINGS):
        if active_pump is not None:
            return None
        readings.append(read_cap_time_us(charge, sense))
        if i < NUM_READINGS - 1:
            time.sleep_ms(100)
    return median(readings)


def record_hourly_sample(pump_id, sample_median_us, seconds_since_start):
    """Buffer one 5-min median for pump_id's pot. Once SAMPLES_PER_HOUR have
    accumulated, flush an aggregate row (mean/min/max) to that pot's log
    file, clear the buffer, and evaluate automatic watering for this pot.
    """
    buf = hourly_buffers[pump_id]
    buf.append(sample_median_us)
    if len(buf) < SAMPLES_PER_HOUR:
        return

    mean_us = sum(buf) / len(buf)
    min_us = min(buf)
    max_us = max(buf)
    hourly_buffers[pump_id] = []

    line = "{},{},{},{}\n".format(seconds_since_start, mean_us, min_us, max_us)
    print(line, end="")
    with open(SENSOR_LOG_FILES[pump_id], "a") as f:
        f.write(line)
        f.flush()

    evaluate_watering(pump_id, mean_us, seconds_since_start)


def evaluate_watering(pump_id, mean_us, seconds_since_start):
    # This evaluation supersedes any earlier queued retry for this pot -
    # either it resolves to a fresh pump start (or a fresh queued retry)
    # below, or an early return here means the pot no longer needs one.
    pending_watering[pump_id] = None

    if mean_us >= THRESHOLDS_US[pump_id]:
        return  # soil wet enough

    last = last_pulse_time[pump_id]
    if last is not None and (seconds_since_start - last) < COOLDOWN_SECONDS:
        return  # still cooling down from this pot's previous auto pulse

    cutoff = seconds_since_start - ROLLING_WINDOW_SECONDS
    pulse_history[pump_id] = [t for t in pulse_history[pump_id] if t > cutoff]
    if len(pulse_history[pump_id]) >= DAILY_CAP:
        return  # daily safety cap reached for this pot

    # start_pump() is the atomic mutual-exclusion gatekeeper - if a
    # button-triggered or the other pot's auto-triggered run is already
    # active, queue this pot to retry the instant that run's stop_pump
    # fires, rather than waiting a full extra hour for its own next flush.
    if start_pump(pump_id):
        last_pulse_time[pump_id] = seconds_since_start
        pulse_history[pump_id].append(seconds_since_start)
    else:
        pending_watering[pump_id] = mean_us


def log_pump_event(pump_id, started):
    line = "{},{},{}\n".format(time.time() - start_time, pump_id, 1 if started else 0)
    print(line, end="")
    with open(PUMP_LOG_FILE, "a") as f:
        f.write(line)
        f.flush()


def disable_button(pump_id):
    # Voltage sag from a running pump can spuriously read as a button press,
    # so the *other* button is fully deaf (IRQ detached, not just ignored)
    # for as long as this pump is active - see the "still recurring"
    # spurious-activation issue in REQUIREMENTS.md. This pump's own button
    # stays live so a long-press can stop it early.
    button_pins[pump_id].irq(handler=None)


def enable_button(pump_id):
    button_pins[pump_id].irq(
        trigger=Pin.IRQ_FALLING | Pin.IRQ_RISING, handler=button_handlers[pump_id]
    )


def start_pump(pump_id):
    """Attempt to start pump_id. Returns True if it actually started, False
    if another pump already held the active_pump lock.

    The active_pump check-and-set is wrapped in disable_irq/enable_irq so a
    button IRQ can't preempt between the check and the set. This matters now
    that both a button IRQ and the (non-IRQ, preemptible) main loop's
    automatic-watering evaluation can call this function - without the
    guard, a button press landing in that gap could start a second pump
    while the first is already running.
    """
    global active_pump
    irq_state = disable_irq()
    if active_pump is not None:
        enable_irq(irq_state)
        return False
    active_pump = pump_id
    enable_irq(irq_state)

    disable_button(2 if pump_id == 1 else 1)
    log_pump_event(pump_id, started=True)
    pumps[pump_id].value(1)
    pump_timers[pump_id].init(
        mode=Timer.ONE_SHOT, period=MAX_PUMP_RUN_MS, callback=lambda t: stop_pump(pump_id)
    )
    return True


def stop_pump(pump_id):
    global active_pump
    pump_timers[pump_id].deinit()  # cancel the max-runtime cap if stopped early
    pumps[pump_id].value(0)
    active_pump = None
    log_pump_event(pump_id, started=False)
    other = 2 if pump_id == 1 else 1
    enable_button(other)

    # If the other pot's auto-trigger was blocked by this run holding the
    # lock, it's left queued in pending_watering for the main loop to pick
    # up on its next idle iteration (see the main loop) - not retried here.
    # This stop_pump call is itself commonly running inside this pump's own
    # MAX_PUMP_RUN_MS Timer callback; retrying synchronously from here would
    # arm the retried pump's Timer (and log its start/eventually its stop)
    # from *inside* that callback - a Timer callback nested inside another
    # Timer callback. A 2026-07-10 field incident traced a 5+ hour total
    # lockup to exactly this: the nested pump's own stop, one level deeper
    # still, physically turned its pump off on schedule but then hung the
    # whole interpreter before its flash log write completed - see Known
    # issues in REQUIREMENTS.md.


def handle_long_press(pump_id):
    if active_pump == pump_id:
        stop_pump(pump_id)
    else:
        start_pump(pump_id)  # no-op if the other pump is somehow active
        # (the other pump's button is deafened while it runs, so that case
        # shouldn't be reachable here - start_pump's own guard is defense
        # in depth, not the primary mechanism, for that scenario)


def make_button_handler(pump_id):
    def handler(pin):
        now = time.ticks_ms()
        if pin.value() == 0:
            # Falling edge: button just pressed down, start timing the hold.
            press_start_ms[pump_id] = now
            return
        # Rising edge: button just released - act only on a long-enough hold.
        held_ms = time.ticks_diff(now, press_start_ms[pump_id])
        if held_ms >= LONG_PRESS_MS:
            handle_long_press(pump_id)
    return handler


button1_handler = make_button_handler(1)
button2_handler = make_button_handler(2)
button_handlers = {1: button1_handler, 2: button2_handler}


ensure_log_header(LOG_FILE_1, SENSOR_LOG_HEADER)
ensure_log_header(LOG_FILE_2, SENSOR_LOG_HEADER)
ensure_log_header(PUMP_LOG_FILE, PUMP_LOG_HEADER)

for led in all_leds:
    led.value(1)
    time.sleep_ms(500)
    led.value(0)

start_time = time.time()

# Arm the button interrupts only after the rest of setup (log files, LED
# flash) has run and power rails have settled, so a boot-time transient
# can't be misread as a button press.
time.sleep_ms(200)
enable_button(1)
enable_button(2)

while True:
    if active_pump is not None:
        # Watering in progress: the Pico does one thing at a time, so
        # postpone sensor reading until the pump is done.
        time.sleep_ms(IDLE_POLL_MS)
        continue

    # Service any auto-trigger that stop_pump left queued because the other
    # pot's pump was running at the time (see pending_watering, stop_pump).
    # Doing the retry here - ordinary main-loop code - rather than inside
    # stop_pump itself means start_pump's Timer.init() and flash write for
    # the retried pump never run nested inside another timer/IRQ callback.
    retried = False
    for pump_id in (1, 2):
        if pending_watering[pump_id] is not None:
            mean_us = pending_watering[pump_id]
            evaluate_watering(pump_id, mean_us, time.time() - start_time)
            retried = True
    if retried:
        continue  # re-check active_pump before falling through to sensors

    seconds_since_start = time.time() - start_time

    median1 = sample_sensor_median(sensor1_charge, sensor1_sense)
    if median1 is None:
        continue  # a button press interrupted this batch; retry once idle

    if active_pump is not None:
        continue  # pump started between the two sensors; skip this cycle

    median2 = sample_sensor_median(sensor2_charge, sensor2_sense)
    if median2 is None:
        continue

    record_hourly_sample(1, median1, seconds_since_start)
    record_hourly_sample(2, median2, seconds_since_start)

    time.sleep(INTERVAL_SECONDS)
