# Hikrobot Web

เว็บแดชบอร์ดสำหรับควบคุมกล้อง **Hikrobot** ผ่าน SDK ร่วมกับ OpenCV/Flask เปิดใช้งานผ่านเบราว์เซอร์ในวง LAN เดียวกัน

## ฟีเจอร์

- ภาพสดจากกล้อง (MJPEG stream) พร้อมแสดงความละเอียดและ FPS
- ปรับ `ExposureTime` และ `Gain` ผ่าน Hikrobot SDK
- ปรับ `Contrast` (ประมวลผลในโปรแกรม เพราะกล้องรุ่นที่ใช้ไม่มี node Contrast)
- **โหมด 1 – อัดวิดีโอ**: กดอัด/หยุด หรือตั้งเวลาหน่วงก่อนเริ่ม และกำหนดความยาวคลิป
- **โหมด 2 – ถ่ายรูปตามช่วงเวลา**: กำหนดความถี่ (วินาที/รูป) และระยะเวลาทำงาน
- ไฟล์ที่ได้บันทึกแยกโฟลเดอร์: รูปภาพ → `images/`, วิดีโอ → `videos/`

## ไฟล์ในโปรเจกต์

| ไฟล์ | หน้าที่ |
| --- | --- |
| `hikrobot_web.py` | แอปหลัก (Flask + Hikrobot SDK + OpenCV) |
| `hikrobot_preview.py` | สคริปต์ทดสอบแสดงภาพพรีวิวจากกล้องแบบง่าย |

## ความต้องการของระบบ

- Windows (อาศัย DLL ของ Hikrobot MVS SDK)
- Hikrobot MVS SDK / Runtime ติดตั้งแล้วที่
  - `C:\Program Files (x86)\Common Files\MVS\Runtime\Win64_x64`
  - `C:\Program Files (x86)\MVS\Development\Samples\Python\MvImport`
- Python 3.8+ และแพ็กเกจ:
  ```bash
  pip install opencv-python numpy flask
  ```

> เส้นทาง SDK ถูกกำหนดไว้ในตัวแปร `dll_dir` และ `sys.path` ด้านบนของ `hikrobot_web.py` หากติดตั้ง SDK ไว้ที่อื่นให้แก้ไขสองบรรทัดนั้น

## วิธีใช้งาน

```bash
python hikrobot_web.py
```

จากนั้นเปิดเบราว์เซอร์ที่ `http://<ip-ของเครื่อง>:8000` (ค่าเริ่มต้นพอร์ต 8000 แก้ได้ที่ตัวแปร `PORT`)

## หมายเหตุ

- โฟลเดอร์ `images/` และ `videos/` จะถูกสร้างอัตโนมัติเมื่อรันครั้งแรก และไฟล์ผลลัพธ์ไม่ถูก track ใน git (ดู `.gitignore`)
