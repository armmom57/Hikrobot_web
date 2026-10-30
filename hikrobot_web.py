# -*- coding: utf-8 -*-
"""Hikrobot USB camera + OpenCV web dashboard

รัน:  python hikrobot_web.py
เปิดเบราว์เซอร์:  http://<ip-ของเครื่อง>:8000

- ภาพสดจากกล้อง (MJPEG) พร้อมความละเอียด/FPS
- ปรับ ExposureTime, Gain ผ่าน SDK และ Contrast (ประมวลผลในโปรแกรม เพราะกล้องรุ่นนี้ไม่มี node Contrast)
- โหมด 1: อัดวิดีโอ กดอัด/หยุด หรือตั้งเวลาหน่วงก่อนเริ่ม + กำหนดความยาว
- โหมด 2: ถ่ายรูปตามความถี่ (วินาที/รูป) และกำหนดความยาว (วินาที/นาที)
- ไฟล์ภาพ -> images/ , ไฟล์วิดีโอ -> videos/ (อยู่ข้างไฟล์สคริปต์นี้)
"""

import copy
import os
import queue
import socket
import sys
import threading
import time
from ctypes import POINTER, cast, string_at

import cv2
import numpy as np
from flask import Flask, Response, jsonify, request


# ให้พิมพ์ข้อความภาษาไทยบน console Windows ได้
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


# ---------- Hikrobot SDK ----------
dll_dir = r"C:\Program Files (x86)\Common Files\MVS\Runtime\Win64_x64"
os.environ["PATH"] = dll_dir + os.pathsep + os.environ["PATH"]
dll_handle = os.add_dll_directory(dll_dir)
sys.path.insert(
    0,
    r"C:\Program Files (x86)\MVS\Development\Samples\Python\MvImport",
)
from MvCameraControl_class import *  # noqa: E402


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGE_DIR = os.path.join(BASE_DIR, "images")
VIDEO_DIR = os.path.join(BASE_DIR, "videos")
os.makedirs(IMAGE_DIR, exist_ok=True)
os.makedirs(VIDEO_DIR, exist_ok=True)

PORT = 8000
JPEG_QUALITY = 72
PREVIEW_FPS = 60        # จำนวนเฟรม/วิที่ส่งให้เบราว์เซอร์ (สูงสุดตาม fps ของกล้อง)
PREVIEW_MAX_WIDTH = 960  # ย่อภาพพรีวิวเพื่อลดขนาดข้อมูล (ไฟล์ที่บันทึกยังเต็มความละเอียด)
RECORD_FPS_CAP = 30     # จำกัด fps ที่เขียนลงวิดีโอ
DEFAULT_FPS = 15
CONTRAST_MIN = 0.2
CONTRAST_MAX = 3.0


def _timestamp(tag):
    now = time.time()
    return (
        time.strftime(f"{tag}_%Y%m%d_%H%M%S", time.localtime(now))
        + f"_{int((now % 1) * 1000):03d}"
    )


class VideoRecorder(threading.Thread):
    """เขียนวิดีโอใน thread แยก เพื่อไม่ให้การเข้ารหัสมาหน่วงการรับภาพ"""

    def __init__(self, path, fps, size):
        super().__init__(daemon=True)
        self.path = path
        self.fps = fps
        self.size = size
        self.frames = 0
        self.error = ""
        self.ready = threading.Event()
        self._latest = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

    def push(self, frame):
        with self._lock:
            self._latest = frame

    def run(self):
        writer = cv2.VideoWriter(
            self.path,
            cv2.VideoWriter_fourcc(*"mp4v"),
            float(self.fps),
            self.size,
            isColor=False,
        )
        if not writer.isOpened():
            self.error = "เปิดไฟล์วิดีโอไม่สำเร็จ (ตรวจสิทธิ์เขียนโฟลเดอร์ videos)"
            self.ready.set()
            return
        self.ready.set()

        gap = 1.0 / self.fps
        next_at = time.monotonic()
        try:
            while not self._stop_event.is_set():
                with self._lock:
                    frame = self._latest
                if frame is None:
                    time.sleep(0.005)
                    continue
                try:
                    writer.write(frame)
                except Exception as exc:
                    self.error = f"เขียนวิดีโอไม่สำเร็จ: {exc}"
                    return
                self.frames += 1
                next_at += gap
                delay = next_at - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_at = time.monotonic()
        finally:
            writer.release()

    def close(self):
        self._stop_event.set()
        self.join(timeout=5)


class CameraWorker(threading.Thread):
    """รับภาพจากกล้องใน thread เดียว แล้วแชร์เฟรมให้เว็บ + จัดการอัด/ถ่ายรูป"""

    def __init__(self):
        super().__init__(daemon=True)
        self.cam = None
        self._created = False
        self._opened = False
        self._grabbing = False
        self._stop_event = threading.Event()
        self.cmds = queue.Queue()

        self.frame_cond = threading.Condition()
        self.frame = None
        self.frame_id = 0

        self.status_lock = threading.Lock()
        self.state = {
            "connected": False,
            "error": "",
            "message": "",
            "width": 0,
            "height": 0,
            "fps": 0.0,
            "exposure": {"min": None, "max": None, "cur": None},
            "gain": {"min": None, "max": None, "cur": None},
            "contrast": 1.0,
            "record": {
                "state": "idle",
                "filename": "",
                "elapsed_s": 0.0,
                "remaining_s": None,
                "frames": 0,
                "fps": 0,
            },
            "capture": {
                "state": "idle",
                "interval_s": 0.0,
                "count": 0,
                "remaining_s": None,
                "next_in_s": None,
            },
        }

        # contrast (software LUT)
        self._contrast = 1.0
        self._lut = None
        self._lut_contrast = None

        # fps วัดจากเฟรมจริง
        self._measured_fps = 0.0
        self._frame_times = []

        # สถานะภายในของการอัดวิดีโอ
        self._recorder = None
        self._record_duration = 0.0
        self._rec_start_at = 0.0
        self._rec_stop_at = None

        # สถานะภายในของการถ่ายรูป
        self._cap_interval = 0.0
        self._cap_end_at = None
        self._cap_next_at = 0.0

    # ---------- helper ----------
    def _state_get(self, key):
        with self.status_lock:
            return copy.deepcopy(self.state[key])

    def _set_message(self, text):
        with self.status_lock:
            self.state["message"] = text

    def _update_record(self, **kwargs):
        with self.status_lock:
            self.state["record"].update(kwargs)

    def _update_capture(self, **kwargs):
        with self.status_lock:
            self.state["capture"].update(kwargs)

    @property
    def measured_fps(self):
        return self._measured_fps

    def status(self):
        with self.status_lock:
            state = copy.deepcopy(self.state)
        state["paths"] = {"images": IMAGE_DIR, "videos": VIDEO_DIR}
        state["files"] = {
            "images": len(os.listdir(IMAGE_DIR)) if os.path.isdir(IMAGE_DIR) else 0,
            "videos": len(os.listdir(VIDEO_DIR)) if os.path.isdir(VIDEO_DIR) else 0,
        }
        return state

    def send_command(self, name, args=None, wait=2.0):
        """ส่งคำสั่งไปให้ thread กล้องทำ แล้วรอผล (คืน False ถ้า thread ไม่ทำงาน)"""
        if not self.is_alive():
            return False
        done = threading.Event()
        self.cmds.put((name, args or {}, done))
        done.wait(wait)
        return True

    # ---------- frame sharing ----------
    def wait_frame(self, last_id, timeout):
        with self.frame_cond:
            if self.frame_id == last_id:
                self.frame_cond.wait(timeout)
            if self.frame is None or self.frame_id == last_id:
                return None, last_id
            return self.frame.copy(), self.frame_id

    def placeholder_jpeg(self):
        img = np.full((360, 640), 24, dtype=np.uint8)
        if self._state_get("error"):
            text = "Camera not connected"
        elif self._state_get("connected"):
            text = "Waiting for frame..."
        else:
            text = "Starting camera..."
        cv2.putText(img, text, (30, 180), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (215, 215, 215), 2)
        cv2.putText(
            img,
            "see console for details",
            (30, 220),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (150, 150, 150),
            1,
        )
        ok, buf = cv2.imencode(".jpg", img)
        return buf.tobytes() if ok else b""

    # ---------- camera ----------
    def _open_camera(self):
        devices = MV_CC_DEVICE_INFO_LIST()
        ret = MvCamera.MV_CC_EnumDevices(MV_USB_DEVICE, devices)
        if ret != 0:
            raise RuntimeError(f"ค้นหากล้องไม่สำเร็จ: {ret:#x}")
        if devices.nDeviceNum == 0:
            raise RuntimeError("ไม่พบกล้อง Hikrobot USB")

        device = cast(devices.pDeviceInfo[0], POINTER(MV_CC_DEVICE_INFO)).contents

        self.cam = MvCamera()
        ret = self.cam.MV_CC_CreateHandle(device)
        if ret != 0:
            raise RuntimeError(f"สร้าง handle ไม่สำเร็จ: {ret:#x}")
        self._created = True

        ret = self.cam.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0)
        if ret != 0:
            raise RuntimeError(
                f"เปิดกล้องไม่สำเร็จ: {ret:#x} (ตรวจว่า MVS ปิดการเชื่อมต่อกล้องแล้ว)"
            )
        self._opened = True

        # ถ้าโปรแกรมก่อนหน้าถูกปิดกลางคัน กล้องอาจค้างสถานะกำลังส่งภาพอยู่
        # สั่งหยุด acquisition ก่อน ไม่งั้นเปลี่ยน PixelFormat จะได้ MV_E_DEV_BUSY
        self.cam.MV_CC_SetCommandValue("AcquisitionStop")

        ret = self.cam.MV_CC_SetEnumValue("TriggerMode", MV_TRIGGER_MODE_OFF)
        if ret != 0:
            raise RuntimeError(f"ตั้ง TriggerMode ไม่สำเร็จ: {ret:#x}")

        ret = self.cam.MV_CC_SetEnumValue("PixelFormat", PixelType_Gvsp_Mono8)
        if ret != 0:
            raise RuntimeError(f"ตั้ง Mono8 ไม่สำเร็จ: {ret:#x}")

        # ปิดโหมดอัตโนมัติ เพื่อให้ปรับ Exposure/Gain เองได้
        self.cam.MV_CC_SetEnumValue("ExposureAuto", 0)
        self.cam.MV_CC_SetEnumValue("GainAuto", 0)

        ret = self.cam.MV_CC_StartGrabbing()
        if ret != 0:
            raise RuntimeError(f"เริ่มรับภาพไม่สำเร็จ: {ret:#x}")
        self._grabbing = True

        with self.status_lock:
            self.state["exposure"] = self._read_float("ExposureTime")
            self.state["gain"] = self._read_float("Gain")

    def _read_float(self, key):
        val = MVCC_FLOATVALUE()
        if self.cam.MV_CC_GetFloatValue(key, val) != 0:
            return {"min": None, "max": None, "cur": None}
        return {
            "min": float(val.fMin),
            "max": float(val.fMax),
            "cur": float(val.fCurValue),
        }

    def _grab_frame(self):
        frame_out = MV_FRAME_OUT()
        if self.cam.MV_CC_GetImageBuffer(frame_out, 200) != 0:
            return None
        try:
            info = frame_out.stFrameInfo
            width = int(info.nWidth)
            height = int(info.nHeight)
            raw = string_at(frame_out.pBufAddr, info.nFrameLen)
            return np.frombuffer(raw, dtype=np.uint8).reshape(height, width).copy()
        finally:
            self.cam.MV_CC_FreeImageBuffer(frame_out)

    def _apply_contrast(self, gray):
        c = self._contrast
        if abs(c - 1.0) < 1e-3:
            return gray
        if self._lut is None or self._lut_contrast != c:
            xs = np.arange(256, dtype=np.float32)
            lut = np.clip((xs - 128.0) * c + 128.0, 0, 255).astype(np.uint8)
            self._lut = lut
            self._lut_contrast = c
        return cv2.LUT(gray, self._lut)

    def _close_camera(self):
        try:
            if self._grabbing:
                self.cam.MV_CC_StopGrabbing()
            if self._opened:
                self.cam.MV_CC_CloseDevice()
            if self._created:
                self.cam.MV_CC_DestroyHandle()
        except Exception:
            pass
        self._grabbing = self._opened = self._created = False

    # ---------- loop ----------
    def run(self):
        try:
            self._open_camera()
        except Exception as exc:
            with self.status_lock:
                self.state["connected"] = False
                self.state["error"] = str(exc)
            with self.frame_cond:
                self.frame_cond.notify_all()
            self._close_camera()
            return

        with self.status_lock:
            self.state["connected"] = True
            self.state["error"] = ""

        try:
            while not self._stop_event.is_set():
                self._process_commands()
                frame = self._grab_frame()
                now = time.monotonic()
                shown = self._publish(frame, now) if frame is not None else None
                self._tick(now, shown)
        except Exception as exc:
            with self.status_lock:
                self.state["connected"] = False
                self.state["error"] = f"การทำงานของกล้องหยุดกลางคัน: {exc}"
        finally:
            self._close_record()
            self._close_camera()

    def _publish(self, frame, now):
        self._frame_times.append(now)
        if len(self._frame_times) > 40:
            self._frame_times.pop(0)
        if len(self._frame_times) >= 2:
            span = self._frame_times[-1] - self._frame_times[0]
            if span > 0:
                self._measured_fps = (len(self._frame_times) - 1) / span

        out = self._apply_contrast(frame)
        with self.frame_cond:
            self.frame = out
            self.frame_id += 1
            self.frame_cond.notify_all()

        with self.status_lock:
            self.state["width"] = int(out.shape[1])
            self.state["height"] = int(out.shape[0])
            self.state["fps"] = round(self._measured_fps, 1)
        return out

    def _tick(self, now, frame):
        self._tick_record(now, frame)
        self._tick_capture(now, frame)

    # ---------- video ----------
    def _tick_record(self, now, frame):
        current = self._state_get("record")["state"]

        if current == "waiting":
            if now < self._rec_start_at:
                self._update_record(elapsed_s=0.0, remaining_s=self._rec_start_at - now)
                return
            if frame is None:
                return
            self._open_record(now)
            if self._recorder is None:  # เปิดไฟล์ไม่ได้ กลับไปสถานะ idle แล้ว
                return
            current = "recording"

        if current == "recording":
            recorder = self._recorder
            if recorder is not None:
                if frame is not None:
                    recorder.push(frame)
                if recorder.error:
                    self._close_record()
                    self._set_message(recorder.error)
                    return
                self._update_record(frames=recorder.frames)

            if self._rec_stop_at is not None and now >= self._rec_stop_at:
                self._close_record()
                self._set_message("อัดวิดีโอครบตามเวลาที่ตั้งไว้")
                return

            remaining = (
                None if self._rec_stop_at is None else max(0.0, self._rec_stop_at - now)
            )
            self._update_record(elapsed_s=now - self._rec_start_at, remaining_s=remaining)

    def _open_record(self, now):
        fps = int(round(self._measured_fps or DEFAULT_FPS))
        fps = max(5, min(RECORD_FPS_CAP, fps))
        name = _timestamp("rec") + ".mp4"
        path = os.path.join(VIDEO_DIR, name)
        height, width = self.frame.shape[:2]

        recorder = VideoRecorder(path, fps, (width, height))
        recorder.start()
        recorder.ready.wait(2.0)
        if recorder.error or not recorder.ready.is_set():
            recorder.close()
            with self.status_lock:
                self.state["record"] = {
                    "state": "idle",
                    "filename": "",
                    "elapsed_s": 0.0,
                    "remaining_s": None,
                    "frames": 0,
                    "fps": 0,
                }
            self._set_message(
                recorder.error or "เปิดไฟล์วิดีโอไม่สำเร็จ (ตรวจสิทธิ์เขียนโฟลเดอร์ videos)"
            )
            return

        self._recorder = recorder
        self._rec_start_at = now
        self._rec_stop_at = (
            now + self._record_duration if self._record_duration > 0 else None
        )
        with self.status_lock:
            self.state["record"] = {
                "state": "recording",
                "filename": name,
                "elapsed_s": 0.0,
                "remaining_s": (
                    self._record_duration if self._record_duration > 0 else None
                ),
                "frames": 0,
                "fps": fps,
            }
        self._set_message(f"เริ่มอัดวิดีโอ: {name}")

    def _close_record(self):
        recorder = self._recorder
        if recorder is not None:
            recorder.close()
            self._recorder = None
            self._update_record(frames=recorder.frames)
        self._rec_start_at = 0.0
        self._rec_stop_at = None
        with self.status_lock:
            if self.state["record"]["state"] != "idle":
                self.state["record"]["state"] = "idle"
                self.state["record"]["remaining_s"] = None

    # ---------- photo ----------
    def _tick_capture(self, now, frame):
        if self._state_get("capture")["state"] != "active":
            return

        remaining = (
            None if self._cap_end_at is None else max(0.0, self._cap_end_at - now)
        )
        if self._cap_end_at is not None and now >= self._cap_end_at:
            self._stop_capture("ถ่ายรูปครบตามเวลาที่ตั้งไว้")
            return

        if frame is None or now < self._cap_next_at:
            self._update_capture(
                remaining_s=remaining,
                next_in_s=max(0.0, self._cap_next_at - now),
            )
            return

        self._save_photo(frame)
        self._cap_next_at = now + self._cap_interval
        self._update_capture(
            remaining_s=remaining,
            next_in_s=max(0.0, self._cap_next_at - now),
        )

    def _save_photo(self, frame):
        name = _timestamp("img") + ".jpg"
        path = os.path.join(IMAGE_DIR, name)
        if not cv2.imwrite(path, frame, [cv2.IMWRITE_JPEG_QUALITY, 95]):
            self._set_message("บันทึกรูปไม่สำเร็จ (ตรวจสิทธิ์เขียนโฟลเดอร์ images)")
            return
        with self.status_lock:
            self.state["capture"]["count"] += 1

    def _stop_capture(self, message=None):
        self._cap_end_at = None
        self._cap_next_at = 0.0
        with self.status_lock:
            self.state["capture"]["state"] = "idle"
            self.state["capture"]["remaining_s"] = None
            self.state["capture"]["next_in_s"] = None
        if message:
            self._set_message(message)

    # ---------- commands ----------
    def _process_commands(self):
        while True:
            try:
                name, args, done = self.cmds.get_nowait()
            except queue.Empty:
                return
            now = time.monotonic()
            try:
                if name == "shutdown":
                    self._stop_event.set()
                elif name == "settings":
                    self._apply_settings(args)
                elif name == "record_start":
                    self._command_record_start(args, now)
                elif name == "record_stop":
                    if self._state_get("record")["state"] != "idle":
                        self._close_record()
                        self._set_message("หยุดอัดวิดีโอแล้ว")
                elif name == "capture_start":
                    self._command_capture_start(args, now)
                elif name == "capture_stop":
                    self._stop_capture("หยุดถ่ายรูปแล้ว")
            except Exception as exc:
                self._set_message(f"คำสั่ง {name} ผิดพลาด: {exc}")
            finally:
                if done is not None:
                    done.set()

    def _apply_settings(self, args):
        messages = []

        value = args.get("exposure_us", None)
        if value not in (None, ""):
            try:
                exposure = float(value)
                ret = self.cam.MV_CC_SetFloatValue("ExposureTime", exposure)
                if ret != 0:
                    messages.append(f"ตั้ง ExposureTime ไม่สำเร็จ ({ret:#x})")
                else:
                    with self.status_lock:
                        self.state["exposure"] = self._read_float("ExposureTime")
                    messages.append(
                        f"ExposureTime = {self._state_get('exposure')['cur']:.0f} µs"
                    )
            except ValueError:
                messages.append("ค่า ExposureTime ไม่ถูกต้อง")

        value = args.get("gain", None)
        if value not in (None, ""):
            try:
                gain = float(value)
                ret = self.cam.MV_CC_SetFloatValue("Gain", gain)
                if ret != 0:
                    messages.append(f"ตั้ง Gain ไม่สำเร็จ ({ret:#x})")
                else:
                    with self.status_lock:
                        self.state["gain"] = self._read_float("Gain")
                    messages.append(f"Gain = {self._state_get('gain')['cur']:.2f} dB")
            except ValueError:
                messages.append("ค่า Gain ไม่ถูกต้อง")

        value = args.get("contrast", None)
        if value not in (None, ""):
            try:
                contrast = min(CONTRAST_MAX, max(CONTRAST_MIN, float(value)))
                self._contrast = contrast
                with self.status_lock:
                    self.state["contrast"] = contrast
                messages.append(f"Contrast = {contrast:.2f} (software)")
            except ValueError:
                messages.append("ค่า Contrast ไม่ถูกต้อง")

        self._set_message(" · ".join(messages) if messages else "ไม่มีค่าที่ต้องปรับ")

    def _command_record_start(self, args, now):
        duration = float(args.get("duration_s") or 0)
        delay = max(0.0, float(args.get("delay_s") or 0))

        self._close_record()
        self._record_duration = max(0.0, duration)
        self._rec_start_at = now + delay

        with self.status_lock:
            self.state["record"] = {
                "state": "waiting",
                "filename": "",
                "elapsed_s": 0.0,
                "remaining_s": delay,
                "frames": 0,
                "fps": 0,
            }
        if delay > 0:
            self._set_message(f"จะเริ่มอัดวิดีโอในอีก {delay:.0f} วินาที")
        else:
            self._set_message("กำลังเริ่มอัดวิดีโอ...")

    def _command_capture_start(self, args, now):
        interval = float(args.get("interval_s") or 1)
        if interval < 0.1:
            interval = 0.1
        duration = max(0.0, float(args.get("duration_s") or 0))

        self._cap_interval = interval
        self._cap_next_at = now
        self._cap_end_at = (now + duration) if duration > 0 else None

        with self.status_lock:
            self.state["capture"] = {
                "state": "active",
                "interval_s": interval,
                "count": 0,
                "remaining_s": duration if duration > 0 else None,
                "next_in_s": 0.0,
            }
        self._set_message(
            f"เริ่มถ่ายรูปทุก {interval:g} วินาที"
            + (f" นาน {duration:g} วินาที" if duration > 0 else " (ไม่จำกัดเวลา)")
        )

    def shutdown(self):
        if self.is_alive():
            self.send_command("shutdown", wait=1.0)
        self._stop_event.set()
        self.join(timeout=5)


worker = CameraWorker()

app = Flask(__name__)


@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html; charset=utf-8")


@app.get("/stream")
def stream():
    resp = Response(
        mjpeg_stream(), mimetype="multipart/x-mixed-replace; boundary=frame"
    )
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@app.get("/api/status")
def api_status():
    return jsonify(worker.status())


@app.post("/api/settings")
def api_settings():
    body = request.get_json(silent=True) or {}
    worker.send_command("settings", body)
    return jsonify(worker.status())


@app.post("/api/record/start")
def api_record_start():
    body = request.get_json(silent=True) or {}
    worker.send_command(
        "record_start",
        {
            "delay_s": body.get("delay_s", 0),
            "duration_s": body.get("duration_s", 0),
        },
    )
    return jsonify(worker.status())


@app.post("/api/record/stop")
def api_record_stop():
    worker.send_command("record_stop")
    return jsonify(worker.status())


@app.post("/api/capture/start")
def api_capture_start():
    body = request.get_json(silent=True) or {}
    worker.send_command(
        "capture_start",
        {
            "interval_s": body.get("interval_s", 1),
            "duration_s": body.get("duration_s", 0),
        },
    )
    return jsonify(worker.status())


@app.post("/api/capture/stop")
def api_capture_stop():
    worker.send_command("capture_stop")
    return jsonify(worker.status())


def mjpeg_stream():
    """MJPEG: ส่งเฉพาะเฟรมล่าสุด และคัดเฟรมทิ้งให้เหลือ PREVIEW_FPS

    ใช้การข้ามเฟรมตามเวลาที่เฟรมมาถึง แทนการ sleep เพราะ time.sleep()
    บน Windows ถูกปัดขึ้นเป็น ~15.6 ms ทำให้ fps ที่ส่งได้ต่ำกว่าที่ตั้ง
    """
    last_id = -1
    gap = 1.0 / PREVIEW_FPS
    next_at = 0.0
    while True:
        frame, frame_id = worker.wait_frame(last_id, timeout=1.0)
        if frame is None:
            data = worker.placeholder_jpeg()
        else:
            last_id = frame_id
            now = time.monotonic()
            # ถ้ากล้องวิ่งไม่เกินเป้าหมาย ส่งทุกเฟรม (ไม่ตัดทิ้ง) เพื่อให้ได้ fps สูงสุด
            # ถ้ากล้องเร็วกว่าเป้าหมาย ค่อยตัดเฟรมให้เหลือประมาณ PREVIEW_FPS
            if worker.measured_fps > PREVIEW_FPS * 1.1 and now + 0.004 < next_at:
                continue
            next_at = now + gap if now >= next_at else next_at + gap

            height, width = frame.shape[:2]
            if width > PREVIEW_MAX_WIDTH:
                scale = PREVIEW_MAX_WIDTH / width
                frame = cv2.resize(
                    frame,
                    (PREVIEW_MAX_WIDTH, max(1, int(round(height * scale)))),
                    interpolation=cv2.INTER_AREA,
                )
            ok, buf = cv2.imencode(
                ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
            )
            if not ok:
                continue
            data = buf.tobytes()
        yield (
            b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
            + str(len(data)).encode()
            + b"\r\n\r\n"
            + data
            + b"\r\n"
        )


def local_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


PAGE = """<!doctype html>
<html lang="th">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>กล้อง Hikrobot · แผงควบคุม</title>
<style>
:root{--paper:#f5f5f1;--panel:#fff;--ink:#23231f;--ink-soft:#53534c;--muted:#8a8a80;--line:#e3e3dc;--line-soft:#eeece6;--danger:#a8412f}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font:14px/1.55 "Segoe UI","Leelawadee UI","Noto Sans Thai",Tahoma,sans-serif;-webkit-font-smoothing:antialiased}
header{display:flex;align-items:center;justify-content:space-between;gap:18px;flex-wrap:wrap;padding:13px 22px;background:var(--panel);border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.brand h1{margin:0;font-size:14px;font-weight:600}
.brand-sub{font-size:11.5px;color:var(--muted)}
.statusline{display:flex;align-items:center;flex-wrap:wrap;font-size:12px;color:var(--ink-soft)}
.chip{padding:0 12px;border-left:1px solid var(--line-soft);white-space:nowrap}
.chip:first-child{border-left:0;padding-left:0}
.chip:last-child{padding-right:0}
#camChip{display:inline-flex;align-items:center;gap:7px}
#camChip::before{content:"";width:7px;height:7px;border-radius:50%;background:#b8b8af}
#camChip.ok::before{background:#3f8f5a}
#camChip.bad::before{background:var(--danger)}
#recChip.rec{color:var(--danger);font-weight:600}
main{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(330px,.9fr);gap:18px;padding:18px 22px 30px;align-items:start}
@media(max-width:980px){main{grid-template-columns:1fr;padding:14px}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px}
.preview{overflow:hidden}
.preview-head{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:11px 15px;border-bottom:1px solid var(--line-soft)}
.preview-title{font-size:12.5px;font-weight:600}
.preview-note{font-size:11.5px;color:var(--muted)}
.viewer{position:relative;background:#0d0d0c}
.viewer img{display:block;width:100%;height:auto;min-height:240px;object-fit:contain}
.badge{position:absolute;top:10px;left:10px;display:flex;gap:6px}
.badge span{font:11px/1 ui-monospace,"Cascadia Mono",Consolas,monospace;letter-spacing:.06em;color:#f3f3ef;background:rgba(0,0,0,.45);border-radius:4px;padding:4px 7px}
.badge .dot{display:inline-block;width:6px;height:6px;border-radius:50%;background:#e05648;margin-right:5px;vertical-align:1px}
.blink{animation:pulse 1.6s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.meta{display:flex;flex-wrap:wrap;gap:3px 18px;padding:9px 15px;border-top:1px solid var(--line-soft);font:11.5px ui-monospace,"Cascadia Mono",Consolas,monospace;color:#6f6f68}
.message{margin:0;padding:0 15px 12px;font-size:12px;color:#9a6a1e}
.message:empty{display:none}
.section{padding:15px}
.section+.section{border-top:1px solid var(--line-soft)}
.section h2{margin:0 0 13px;font-size:12.5px;font-weight:600}
.note{font-size:10.5px;color:var(--muted);font-weight:400}
.setting+.setting{margin-top:14px}
.setting-head{display:flex;align-items:baseline;justify-content:space-between;gap:8px;margin-bottom:6px}
.setting-head label{font-size:12px;color:var(--ink-soft)}
.cur{font:11px ui-monospace,"Cascadia Mono",Consolas,monospace;color:var(--muted)}
.setting-body{display:flex;align-items:center;gap:10px}
input[type=range]{flex:1;min-width:0;height:18px;margin:0;accent-color:#3a3a32}
.num{display:flex;align-items:center;flex:0 0 auto;border:1px solid var(--line);border-radius:5px;background:#fbfbf8}
.num input[type=number]{width:64px;border:0;background:transparent;padding:5px 6px;text-align:right;font:12px ui-monospace,"Cascadia Mono",Consolas,monospace;color:var(--ink)}
.num span{padding:0 8px;border-left:1px solid var(--line-soft);font-size:11px;color:var(--muted);line-height:26px;white-space:nowrap}
.num select{border:0;background:transparent;font:12px "Segoe UI","Leelawadee UI",Tahoma,sans-serif;color:var(--ink-soft);padding:5px 4px;border-left:1px solid var(--line-soft);outline:0}
input[type=number]:focus{outline:1px solid #a8a89e;outline-offset:1px}
.fields{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.field label{display:block;margin-bottom:5px;font-size:11.5px;color:var(--muted)}
.fields .num{width:100%}
.fields .num input[type=number]{flex:1;width:auto}
.actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.btn{flex:1;padding:7px 12px;border:1px solid var(--line);border-radius:5px;background:#fdfdfb;color:var(--ink-soft);font:12.5px "Segoe UI","Leelawadee UI",Tahoma,sans-serif;cursor:pointer}
.btn:hover{background:#f4f4ee;color:var(--ink)}
.btn.primary{background:#26261f;border-color:#26261f;color:#fbfbf7}
.btn.primary:hover{background:#3a3a31;color:#fff}
.btn.danger{color:var(--danger);border-color:#e6d7d3;background:#fdfbfa}
.btn.danger:hover{background:#f8f0ee}
.btn:disabled{opacity:.42;cursor:default}
.btn:disabled:hover{background:#fdfdfb;color:var(--ink-soft)}
.btn.primary:disabled:hover{background:#26261f;color:#fbfbf7}
.btn.danger:disabled:hover{background:#fdfbfa}
.hint{margin:7px 0 0;font-size:11.5px;line-height:1.5;color:var(--muted)}
.status{margin-top:11px;padding:8px 10px;border:1px solid var(--line-soft);border-radius:5px;background:#fbfbf8;font-size:12px;line-height:1.6;color:var(--ink-soft);min-height:38px;overflow-wrap:anywhere}
.status b{font-weight:600;color:var(--ink)}
</style>
</head>
<body>
<header>
  <div class="brand">
    <h1>กล้อง Hikrobot</h1>
    <span class="brand-sub">แผงควบคุมผ่านเว็บ</span>
  </div>
  <div class="statusline">
    <span class="chip" id="camChip">กำลังเชื่อมต่อ</span>
    <span class="chip" id="resChip">—</span>
    <span class="chip" id="fpsChip">FPS —</span>
    <span class="chip" id="recChip">ไม่ได้อัด</span>
  </div>
</header>

<main>
  <section class="card preview">
    <div class="preview-head">
      <span class="preview-title">ภาพจากกล้อง</span>
      <span class="preview-note">Mono8 · ภาพสด</span>
    </div>
    <div class="viewer">
      <img id="stream" src="/stream" alt="ภาพจากกล้อง">
      <div class="badge">
        <span id="liveBadge">LIVE</span>
        <span id="recBadge" hidden><span class="dot blink"></span>REC</span>
      </div>
    </div>
    <div class="meta">
      <span id="metaRes">ความละเอียด: -</span>
      <span id="metaFps">FPS: -</span>
      <span id="metaExp">Exposure: -</span>
      <span id="metaGain">Gain: -</span>
      <span id="counter">รูป: 0 · วิดีโอ: 0</span>
    </div>
    <div class="message" id="message"></div>
  </section>

  <section class="card">
    <div class="section">
      <h2>ค่ากล้อง</h2>

      <div class="setting">
        <div class="setting-head">
          <label for="expNum">Exposure Time</label>
          <span class="cur" id="expCur"></span>
        </div>
        <div class="setting-body">
          <input type="range" id="expRange" min="30" max="999990" step="10" value="2000">
          <div class="num"><input type="number" id="expNum" step="10" value="2000"><span>µs</span></div>
        </div>
      </div>

      <div class="setting">
        <div class="setting-head">
          <label for="gainNum">Gain</label>
          <span class="cur" id="gainCur"></span>
        </div>
        <div class="setting-body">
          <input type="range" id="gainRange" min="0" max="20.03" step="0.1" value="0">
          <div class="num"><input type="number" id="gainNum" step="0.1" value="0"><span>dB</span></div>
        </div>
      </div>

      <div class="setting">
        <div class="setting-head">
          <label for="conNum">Contrast <span class="note">ประมวลผลในโปรแกรม</span></label>
          <span class="cur" id="conCur"></span>
        </div>
        <div class="setting-body">
          <input type="range" id="conRange" min="0.2" max="3" step="0.05" value="1">
          <div class="num"><input type="number" id="conNum" step="0.05" value="1"><span>×</span></div>
        </div>
      </div>

      <p class="hint">เลื่อนหรือพิมพ์ค่าแล้วมีผลทันที ไม่ต้องกด Apply</p>
    </div>

    <div class="section">
      <h2>อัดวิดีโอ</h2>
      <div class="fields">
        <div class="field">
          <label for="recDelay">หน่วงก่อนเริ่ม</label>
          <div class="num"><input type="number" id="recDelay" value="0" min="0" step="1"><span>วินาที</span></div>
        </div>
        <div class="field">
          <label for="recDur">ความยาว</label>
          <div class="num">
            <input type="number" id="recDur" value="0" min="0" step="1">
            <select id="recUnit" aria-label="หน่วยความยาววิดีโอ">
              <option value="s">วินาที</option>
              <option value="m">นาที</option>
            </select>
          </div>
        </div>
      </div>
      <p class="hint">0 = อัดจนกว่าจะกดหยุด · ใส่ช่องหน่วงเพื่อให้เริ่มอัดเองตามเวลา</p>
      <div class="actions">
        <button class="btn primary" id="recStart">เริ่มอัด</button>
        <button class="btn danger" id="recStop" disabled>หยุดอัด</button>
      </div>
      <div class="status" id="recStatus">ยังไม่ได้อัดวิดีโอ</div>
    </div>

    <div class="section">
      <h2>ถ่ายรูปตามช่วงเวลา</h2>
      <div class="fields">
        <div class="field">
          <label for="capInterval">ความถี่</label>
          <div class="num"><input type="number" id="capInterval" value="1" min="0.1" step="0.1"><span>วินาที/รูป</span></div>
        </div>
        <div class="field">
          <label for="capDur">ความยาว</label>
          <div class="num">
            <input type="number" id="capDur" value="10" min="0" step="1">
            <select id="capUnit" aria-label="หน่วยระยะเวลาถ่ายรูป">
              <option value="s">วินาที</option>
              <option value="m" selected>นาที</option>
            </select>
          </div>
        </div>
      </div>
      <p class="hint">เช่น 1 วินาที/รูป นาน 10 นาที · 0 = ถ่ายจนกว่าจะกดหยุด</p>
      <div class="actions">
        <button class="btn primary" id="capStart">เริ่มถ่ายรูป</button>
        <button class="btn danger" id="capStop" disabled>หยุดถ่ายรูป</button>
      </div>
      <div class="status" id="capStatus">ยังไม่เริ่มถ่ายรูป</div>
    </div>
  </section>
</main>

<script>
const $ = (id) => document.getElementById(id);
const num = (v, d = 1) => (v === null || v === undefined ? "-" : Number(v).toFixed(d));
const fmtTime = (s) => {
  s = Math.max(0, Math.round(s));
  const m = Math.floor(s / 60), r = s % 60;
  return m ? `${m} นาที ${r} วิ` : `${r} วิ`;
};

// ปรับค่าแล้วส่งไปกล้องทันที แบบ throttle เพื่อไม่ให้ยิงคำสั่งถี่เกินไป
let pendingSettings = {};
let sendingSettings = false;
let settingsTimer = null;
const SETTINGS_GAP_MS = 120;

function queueSetting(key, value) {
  if (value === null || value === undefined || Number.isNaN(value)) return;
  pendingSettings[key] = value;
  if (!settingsTimer && !sendingSettings) {
    settingsTimer = setTimeout(flushSettings, SETTINGS_GAP_MS);
  }
}

async function flushSettings() {
  settingsTimer = null;
  if (sendingSettings || !Object.keys(pendingSettings).length) return;
  const body = pendingSettings;
  pendingSettings = {};
  sendingSettings = true;
  try {
    render(await post("/api/settings", body));
  } catch (err) {
    // ปล่อยไว้ เงียบ ๆ เพราะ refresh() จะรายงานสถานะการเชื่อมต่อให้เอง
  } finally {
    sendingSettings = false;
    if (Object.keys(pendingSettings).length) {
      settingsTimer = setTimeout(flushSettings, 80);
    }
  }
}

function clampTo(el, value) {
  const min = el.min === "" ? -Infinity : Number(el.min);
  const max = el.max === "" ? Infinity : Number(el.max);
  return Math.min(max, Math.max(min, value));
}

function bindPair(range, box, key) {
  range.addEventListener("input", () => {
    box.value = range.value;
    queueSetting(key, clampTo(range, Number(range.value)));
  });
  box.addEventListener("input", () => {
    if (box.value === "") return;
    const value = Number(box.value);
    if (Number.isNaN(value)) return;
    range.value = value;
    queueSetting(key, clampTo(range, value));
  });
}
bindPair($("expRange"), $("expNum"), "exposure_us");
bindPair($("gainRange"), $("gainNum"), "gain");
bindPair($("conRange"), $("conNum"), "contrast");

async function post(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return res.json();
}

function durationOf(numId, unitId) {
  const value = Number($(numId).value || 0);
  return value * ($(unitId).value === "m" ? 60 : 1);
}

let settingsLoaded = false;

function render(st) {
  if (!st) return;
  const connected = !!st.connected;

  const chip = $("camChip");
  if (connected) {
    chip.textContent = "เชื่อมต่อแล้ว";
    chip.className = "chip ok";
  } else if (st.error) {
    chip.textContent = "กล้องผิดพลาด";
    chip.className = "chip bad";
  } else {
    chip.textContent = "กำลังเชื่อมต่อ";
    chip.className = "chip";
  }

  $("resChip").textContent = st.width ? `${st.width} × ${st.height}` : "—";
  $("fpsChip").textContent = st.fps ? `FPS ${num(st.fps)}` : "FPS —";
  $("metaRes").textContent = st.width ? `ความละเอียด: ${st.width} × ${st.height}` : "ความละเอียด: -";
  $("metaFps").textContent = `FPS: ${st.fps ? num(st.fps) : "-"}`;
  $("metaExp").textContent = st.exposure && st.exposure.cur !== null
    ? `Exposure: ${Math.round(st.exposure.cur)} µs` : "Exposure: -";
  $("metaGain").textContent = st.gain && st.gain.cur !== null
    ? `Gain: ${num(st.gain.cur, 2)} dB` : "Gain: -";
  $("counter").textContent = `รูป: ${st.files.images} · วิดีโอ: ${st.files.videos}`;
  $("message").textContent = st.error ? `กล้องผิดพลาด: ${st.error}` : (st.message || "");

  if (st.exposure && st.exposure.cur !== null) {
    $("expCur").textContent = `ปัจจุบัน ${Math.round(st.exposure.cur)}`;
  }
  if (st.gain && st.gain.cur !== null) {
    $("gainCur").textContent = `ปัจจุบัน ${num(st.gain.cur, 2)}`;
  }
  $("conCur").textContent = `ปัจจุบัน ${num(st.contrast, 2)}`;

  if (!settingsLoaded && st.exposure && st.exposure.cur !== null) {
    const e = st.exposure;
    $("expRange").min = Math.floor(e.min);
    $("expRange").max = Math.ceil(e.max);
    $("expNum").min = Math.floor(e.min);
    $("expNum").max = Math.ceil(e.max);
    $("expRange").value = Math.round(e.cur);
    $("expNum").value = Math.round(e.cur);

    const g = st.gain;
    if (g && g.cur !== null) {
      $("gainRange").min = g.min;
      $("gainRange").max = g.max;
      $("gainNum").min = g.min;
      $("gainNum").max = g.max;
      $("gainRange").value = Number(g.cur.toFixed(2));
      $("gainNum").value = Number(g.cur.toFixed(2));
    }
    $("conRange").value = st.contrast;
    $("conNum").value = Number(st.contrast.toFixed(2));
    settingsLoaded = true;
  }

  const rec = st.record;
  $("recBadge").hidden = rec.state !== "recording";
  const recChip = $("recChip");
  if (rec.state === "recording") {
    recChip.textContent = "กำลังอัด";
    recChip.className = "chip rec";
    $("recStatus").innerHTML =
      `<b>กำลังอัด</b> · ${fmtTime(rec.elapsed_s)}` +
      (rec.remaining_s !== null ? ` · เหลือ ${fmtTime(rec.remaining_s)}` : " · ไม่จำกัดเวลา") +
      `<br>ไฟล์: videos/${rec.filename} · ${rec.frames} เฟรม @ ${rec.fps} fps`;
    $("recStart").disabled = true;
    $("recStop").disabled = false;
  } else if (rec.state === "waiting") {
    recChip.textContent = "รอเริ่มอัด";
    recChip.className = "chip";
    $("recStatus").innerHTML =
      `กำลังรอเริ่มอัด` +
      (rec.remaining_s !== null ? ` · อีก ${fmtTime(rec.remaining_s)}` : "");
    $("recStart").disabled = true;
    $("recStop").disabled = false;
  } else {
    recChip.textContent = "ไม่ได้อัด";
    recChip.className = "chip";
    $("recStatus").innerHTML = rec.filename
      ? `อัดเสร็จแล้ว: videos/${rec.filename} · ${rec.frames} เฟรม`
      : "ยังไม่ได้อัดวิดีโอ";
    $("recStart").disabled = false;
    $("recStop").disabled = true;
  }

  const cap = st.capture;
  if (cap.state === "active") {
    $("capStatus").innerHTML =
      `<b>กำลังถ่ายรูป</b> · ได้แล้ว ${cap.count} รูป` +
      (cap.remaining_s !== null ? ` · เหลือ ${fmtTime(cap.remaining_s)}` : " · ไม่จำกัดเวลา") +
      `<br>รูปถัดไปใน ${num(cap.next_in_s)} วิ · ทุก ${num(cap.interval_s)} วิ`;
    $("capStart").disabled = true;
    $("capStop").disabled = false;
  } else {
    $("capStatus").innerHTML = cap.count
      ? `หยุดแล้ว · ถ่ายได้ ${cap.count} รูป (images/)`
      : "ยังไม่เริ่มถ่ายรูป";
    $("capStart").disabled = false;
    $("capStop").disabled = true;
  }
}

$("recStart").onclick = async () =>
  render(await post("/api/record/start", {
    delay_s: Number($("recDelay").value || 0),
    duration_s: durationOf("recDur", "recUnit"),
  }));

$("recStop").onclick = async () => render(await post("/api/record/stop", {}));

$("capStart").onclick = async () =>
  render(await post("/api/capture/start", {
    interval_s: Number($("capInterval").value || 1),
    duration_s: durationOf("capDur", "capUnit"),
  }));

$("capStop").onclick = async () => render(await post("/api/capture/stop", {}));

async function refresh() {
  try {
    const res = await fetch("/api/status");
    if (!res.ok) {
      throw new Error("status " + res.status);
    }
    render(await res.json());
  } catch (err) {
    const chip = $("camChip");
    chip.textContent = "เซิร์ฟเวอร์ขาดการเชื่อมต่อ";
    chip.className = "chip bad";
  }
}

refresh();
setInterval(refresh, 1000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    worker.start()
    time.sleep(0.5)
    print("=" * 60)
    print(f"เปิดเบราว์เซอร์ที่  http://{local_ip()}:{PORT}")
    print(f"หรือในเครื่องนี้    http://127.0.0.1:{PORT}")
    print(f"รูปภาพ  -> {IMAGE_DIR}")
    print(f"วิดีโอ  -> {VIDEO_DIR}")
    print("กด Ctrl+C เพื่อปิด")
    print("=" * 60)
    try:
        app.run(host="0.0.0.0", port=PORT, threaded=True, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        pass
    finally:
        worker.shutdown()
        print("ปิดระบบแล้ว")
