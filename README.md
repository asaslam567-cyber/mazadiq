# مزاد محمد الفضلي للساعات الأصلية

موقع عربي (RTL) لإدارة وتجارة ساعات اليد الفاخرة والمزادات الحية.

## التشغيل

```bash
cd ~/Projects/mazad-alfadhli
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 app.py
```

ثم افتح: http://127.0.0.1:5050

## الإدارة

- الرابط: `/admin/login`
- كلمة المرور الافتراضية: `alfadhli2026`

يمكن تغييرها عبر المتغير `ADMIN_PASSWORD`.
