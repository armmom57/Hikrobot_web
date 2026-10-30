import os
import sys
from ctypes import POINTER, cast, string_at
import cv2
import numpy as np


dll_dir = r"C:\Program Files (x86)\Common Files\MVS\Runtime\Win64_x64"
os.environ["PATH"] = dll_dir + os.pathsep + os.environ["PATH"]
dll_handle = os.add_dll_directory(dll_dir)
sys.path.insert(
    0,
    r"C:\Program Files (x86)\MVS\Development\Samples\Python\MvImport"
)
from MvCameraControl_class import *

devices = MV_CC_DEVICE_INFO_LIST()
ret = MvCamera.MV_CC_EnumDevices(MV_USB_DEVICE, devices)

if ret != 0:
    raise RuntimeError(f"ค้นหากล้องไม่สำเร็จ: {ret:#x}")
if devices.nDeviceNum == 0:
    raise RuntimeError("ไม่พบกล้อง Hikrobot USB")

device = cast(
    devices.pDeviceInfo[0],
    POINTER(MV_CC_DEVICE_INFO)
).contents

cam = MvCamera()
created = opened = grabbing = False

try:
    ret = cam.MV_CC_CreateHandle(device)
    if ret != 0:
        raise RuntimeError(f"สร้าง handle ไม่สำเร็จ: {ret:#x}")
    created = True

    ret = cam.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0)
    if ret != 0:
        raise RuntimeError(
            f"เปิดกล้องไม่สำเร็จ: {ret:#x} "
            "(ตรวจว่า MVS ปิดการเชื่อมต่อกล้องแล้ว)"
        )
    opened = True

    ret = cam.MV_CC_SetEnumValue(
        "TriggerMode", MV_TRIGGER_MODE_OFF
    )
    if ret != 0:
        raise RuntimeError(f"ตั้ง TriggerMode ไม่สำเร็จ: {ret:#x}")

    ret = cam.MV_CC_SetEnumValue(
        "PixelFormat", PixelType_Gvsp_Mono8
    )
    if ret != 0:
        raise RuntimeError(f"ตั้ง Mono8 ไม่สำเร็จ: {ret:#x}")

    ret = cam.MV_CC_StartGrabbing()
    if ret != 0:
        raise RuntimeError(f"เริ่มรับภาพไม่สำเร็จ: {ret:#x}")
    grabbing = True







    cv2.namedWindow("Camera", cv2.WINDOW_NORMAL)
    cv2.namedWindow("Histogram", cv2.WINDOW_NORMAL)

    # ปรับค่าได้ด้วยแถบเลื่อนในหน้าต่าง Histogram
    cv2.createTrackbar(
        "High cutoff", "Histogram", 220, 255, lambda value: None
    )

    while True:
        frame_out = MV_FRAME_OUT()
        ret = cam.MV_CC_GetImageBuffer(frame_out, 1000)

        if ret != 0:
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
            continue

        try:
            info = frame_out.stFrameInfo
            width = int(info.nWidth)
            height = int(info.nHeight)

            
            raw = string_at(frame_out.pBufAddr, info.nFrameLen)
            frame = np.frombuffer(
                raw, dtype=np.uint8
            ).reshape(height, width)
        finally:
            cam.MV_CC_FreeImageBuffer(frame_out)

        cutoff = cv2.getTrackbarPos("High cutoff", "Histogram")

        
        hist = cv2.calcHist(
            [frame], [0], None, [256], [0, 256]
        ).reshape(-1)

        
        shown_hist = hist.copy()
        shown_hist[cutoff + 1:] = 0

        high_percent = 100.0 * np.mean(frame > cutoff)

        
        graph = np.zeros((320, 560, 3), dtype=np.uint8)
        max_count = max(float(shown_hist.max()), 1.0)

        points = []
        for gray in range(cutoff + 1):
            x = 35 + gray * 2
            y = 275 - int(
                float(shown_hist[gray]) / max_count * 220
            )
            points.append((x, y))

        if len(points) >= 2:
            cv2.polylines(
                graph,
                [np.array(points, dtype=np.int32)],
                False,
                (0, 220, 0),
                1,
            )

       
        cutoff_x = 35 + cutoff * 2
        cv2.line(
            graph, (cutoff_x, 45), (cutoff_x, 275),
            (0, 0, 255), 1
        )
        cv2.line(
            graph, (35, 275), (545, 275),
            (160, 160, 160), 1
        )

        cv2.putText(
            graph,
            f"Cutoff: {cutoff}  Above: {high_percent:.2f}%",
            (35, 25),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (230, 230, 230),
            1,
        )
        cv2.putText(
            graph,
            "0", (30, 300),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5, (230, 230, 230), 1
        )
        cv2.putText(
            graph,
            "255", (510, 300),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5, (230, 230, 230), 1
        )

        adjusted_frame = np.minimum(frame, cutoff).astype(np.uint8)
        cv2.imshow("Camera", adjusted_frame)
        cv2.imshow("Histogram", graph)

        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

finally:
    if grabbing:
        cam.MV_CC_StopGrabbing()
    if opened:
        cam.MV_CC_CloseDevice()
    if created:
        cam.MV_CC_DestroyHandle()
    cv2.destroyAllWindows()