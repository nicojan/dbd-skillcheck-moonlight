import os
from time import time, sleep
import gradio as gr

from dbd.AI_model import AI_model
from dbd.utils.directkeys import PressKey, ReleaseKey, SPACE
from dbd.utils.monitoring_mss import Monitoring_mss

try:
    from dbd.utils.monitoring_window import Monitoring_window
    window_capture_ok = True
    print("Info: window-targeted capture available (moonlight).")
except ImportError:
    window_capture_ok = False

try:
    from dbd.utils.monitoring_bettercam import Monitoring_bettercam
    bettercam_ok = True
    print("Info: Bettercam feature available.")
except ImportError:
    bettercam_ok = False


ai_model = None
def cleanup():
    global ai_model
    if ai_model is not None:
        del ai_model
        ai_model = None
    return 0.


def monitor(ai_model_path, device, monitoring_str, monitor_id, hit_ante, nb_cpu_threads):
    if ai_model_path is None or not os.path.exists(ai_model_path):
        raise gr.Error("Invalid AI model file", duration=0)

    if device is None:
        raise gr.Error("Invalid device option")

    # Window capture resolves its own geometry, so no monitor selection is needed
    if monitor_id is None and not (monitoring_str == "moonlight" and window_capture_ok):
        raise gr.Error("Invalid monitor option")

    use_gpu = (device == devices[1])

    if monitoring_str == "moonlight" and window_capture_ok:
        # Track the Moonlight window rather than a fixed display region, so the crop
        # follows the stream if the window is moved, resized or windowed.
        monitoring = Monitoring_window(window_query="Moonlight", crop_size=224)
        print("Window capture geometry:", monitoring.describe())
    elif monitoring_str == "bettercam" and bettercam_ok:
        monitoring = Monitoring_bettercam(monitor_id=monitor_id, crop_size=224, target_fps=240)
    else:
        monitoring = Monitoring_mss(monitor_id=monitor_id, crop_size=224)

    try:
        global ai_model
        ai_model = AI_model(ai_model_path, use_gpu, nb_cpu_threads, monitoring)
        execution_provider = ai_model.check_provider()
    except Exception as e:
        raise gr.Error("Error when loading AI model: {}".format(e), duration=0)

    if execution_provider == "CUDAExecutionProvider":
        gr.Info("Running AI model on GPU (success, CUDA)")
    elif execution_provider == "DmlExecutionProvider":
        gr.Info("Running AI model on GPU (success, DirectML)")
    elif execution_provider == "TensorRT":
        gr.Info("Running AI model on GPU (success, TensorRT)")
    else:
        gr.Info(f"Running AI model on CPU (success, {nb_cpu_threads} threads)")
        if use_gpu:
            Warning("Could not run AI model on GPU device. Check python console logs to debug.")

    # Variables
    t0 = time()
    nb_frames = 0

    # Only inject keys while the stream is focused — otherwise SPACE lands in whatever
    # app you switched to. Window capture implies a focusable target, so gate on it.
    focus_watcher = None
    if monitoring_str == "moonlight" and window_capture_ok:
        from dbd.utils.focus_watcher import FocusWatcher
        focus_watcher = FocusWatcher(query="Moonlight")

    try:
        while True:
            if focus_watcher is not None and not focus_watcher.is_active():
                ReleaseKey(SPACE)  # never leave the key held in another app
                sleep(0.2)
                t0 = time()
                nb_frames = 0
                continue

            frame_np = ai_model.grab_screenshot()
            nb_frames += 1

            pred, desc, probs, should_hit = ai_model.predict(frame_np)

            if should_hit:
                # ante-frontier hit delay
                if pred == 2 and hit_ante > 0:
                    sleep(hit_ante * 0.001)

                PressKey(SPACE)
                sleep(0.005)
                ReleaseKey(SPACE)

                yield gr.skip(), frame_np, probs

                sleep(0.5)  # avoid hitting the same skill check multiple times
                t0 = time()
                nb_frames = 0
                continue

            # Compute fps
            t_diff = time() - t0
            if t_diff > 1.0:
                fps = round(nb_frames / t_diff, 1)
                yield fps, gr.skip(), gr.skip()

                t0 = time()
                nb_frames = 0

    except Exception as e:
        # print(e)
        pass
    finally:
        print("Monitoring stopped.")


if __name__ == "__main__":
    models_folder = "models"

    fps_info = "Number of frames per second the AI model analyses the monitored frame."
    devices = ["CPU (default)", "GPU"]
    cpu_choices = [("Low", 2), ("Normal", 4), ("High", 6), ("CPU BBQ Mode", 8)]

    # Find available AI models
    model_files = [(f, f'{models_folder}/{f}') for f in os.listdir(f"{models_folder}/") if f.endswith(".onnx") or f.endswith(".trt")]
    if len(model_files) == 0:
        raise gr.Error(f"No AI model found in {models_folder}/", duration=0)

    # Monitoring
    monitoring_choices = ["mss"]
    if bettercam_ok:
        monitoring_choices.append("bettercam")
    if window_capture_ok:
        monitoring_choices.append("moonlight")

    def switch_monitoring_cb(monitoring_str):
        if monitoring_str == "moonlight" and window_capture_ok:
            monitor_choices = Monitoring_window.get_monitors_info()
        elif monitoring_str == "bettercam" and bettercam_ok:
            monitor_choices = Monitoring_bettercam.get_monitors_info()
        else:
            monitor_choices = Monitoring_mss.get_monitors_info()

        return gr.update(choices=monitor_choices, value=None), None

    # Monitor selection
    monitor_choices = Monitoring_mss.get_monitors_info()
    def switch_monitor_cb(monitoring_str, monitor_id):
        if monitoring_str == "moonlight" and window_capture_ok:
            with Monitoring_window(window_query="Moonlight", crop_size=520) as mon:
                return mon.get_frame_np()
        if monitoring_str == "bettercam" and bettercam_ok:
            with Monitoring_bettercam(monitor_id, crop_size=520) as mon:
                return mon.get_frame_np()
        else:
            with Monitoring_mss(monitor_id, crop_size=520) as mon:
                return mon.get_frame_np()

    with (gr.Blocks(title="Auto skill check") as webui):
        gr.Markdown("<h1 style='text-align: center;'>DBD Auto skill check</h1>", elem_id="title")
        gr.Markdown("https://github.com/Manuteaa/dbd_autoSkillCheck")

        with gr.Row():
            with gr.Column(variant="panel"):
                with gr.Column(variant="panel"):
                    gr.Markdown("AI inference settings")
                    ai_model_path = gr.Dropdown(choices=model_files, value=model_files[0][1], label="Name the AI model to use (ONNX or TensorRT Engine)")
                    device = gr.Radio(choices=devices, value=devices[0], label="Device the AI model will use")
                    with gr.Row():
                        monitoring_str = gr.Dropdown(choices=monitoring_choices, value=monitoring_choices[0], label="Screen monitoring library")
                        monitor_id = gr.Dropdown(choices=monitor_choices, value=monitor_choices[0][1], label="Monitor to use")
                with gr.Column(variant="panel"):
                    gr.Markdown("AI Features options")
                    hit_ante = gr.Slider(minimum=0, maximum=50, step=5, value=20, label="Ante-frontier hit delay in ms")
                    cpu_stress = gr.Radio(
                        label="CPU workload for AI model inference (increase to improve AI model FPS or decrease to reduce CPU stress)",
                        choices=cpu_choices,
                        value=cpu_choices[1][1],
                    )
                with gr.Column():
                    run_button = gr.Button("RUN", variant="primary")
                    stop_button = gr.Button("STOP", variant="stop")

            with gr.Column(variant="panel"):
                fps = gr.Number(label="AI model FPS", info=fps_info, interactive=False)
                image_visu = gr.Image(label="Last hit skill check frame", height=224, interactive=False)
                probs = gr.Label(label="Skill check AI recognition")

        monitoring = run_button.click(
            fn=monitor, 
            inputs=[ai_model_path, device, monitoring_str, monitor_id, hit_ante, cpu_stress],
            outputs=[fps, image_visu, probs]
        )

        stop_button.click(fn=cleanup, inputs=None, outputs=fps)
        monitoring_str.blur(fn=switch_monitoring_cb, inputs=[monitoring_str], outputs=[monitor_id, image_visu])  # triggered when selection is closed
        monitor_id.blur(fn=switch_monitor_cb, inputs=[monitoring_str, monitor_id], outputs=image_visu)  # triggered when selection is closed

    try:
        webui.launch()
    except:
        print("User stopped the web UI. Please wait to cleanup resources...")
    finally:
        cleanup()
