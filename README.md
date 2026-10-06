# Solana Meme Radar V5.1 — High Precision + Learning

این نسخه از سیگنال‌های قبلی یاد می‌گیرد، اما **بدون look-ahead leakage**:
1. هر scan یک snapshot ذخیره می‌کند.
2. Outcome Engine بازده 5m/15m/1h/4h/24h را از snapshotهای بعدی همان توکن استخراج می‌کند.
3. بعد از حداقل 120 نمونه دارای outcome، Logistic Regression برای افق‌های 15m/1h/4h آموزش می‌بیند.
4. split داده‌ها زمانی است؛ 75% اول train و 25% آخر validation.
5. هدف آموزشی محافظه‌کارانه است: آیا بازده آینده حداقل +50% بوده؟
6. ML فقط Alpha را تعدیل می‌کند؛ Risk/Security gate همچنان حاکم است.

### مهم
این مدل «احتمال سود تضمینی» نیست. AUC/Brier صرفاً کیفیت مدل روی validation تاریخی را نشان می‌دهند. برای یادگیری پایدارتر باید صدها تا هزاران snapshot با market regimeهای مختلف جمع شود.

### مرحله بعد
بهترین ارتقای بعدی: اتصال Helius Parsed Events/Streams برای تشخیص swap واقعی Smart Money و سپس اضافه کردن walk-forward retraining و feature importance.
