# انتقال یک ZIP سالانه از GitHub Release به Hugging Face

این نسخه با دریافت **لینک مستقیم دانلود ZIP در GitHub Release** عملیات زیر را انجام می‌دهد:

1. دانلود ZIP از لینک واردشده
2. استخراج فایل `.BIN` سالانه
3. تقسیم Tickها بر اساس روز UTC
4. تولید فایل‌های `.BIN` روزانه
5. آپلود گروهی به Hugging Face
6. ادغام و به‌روزرسانی `index.txt`

## فایل‌ها

```text
.github/workflows/migrate-release-url-to-hf.yml
tools/migrate_release_url.py
```

## تنظیمات Repository

در Settings → Secrets and variables → Actions ایجاد کنید:

### Secret

```text
HF_TOKEN
```

### Variable

```text
HF_DATASET = Esmaeil9ss/Tickdata
```

## لینک قابل قبول

باید لینک مستقیم خود فایل ZIP باشد، مانند:

```text
https://github.com/OWNER/REPO/releases/download/TAG/JPMUSUSD_2023.zip
```

لینک صفحه Release مانند مسیر زیر قابل قبول نیست:

```text
https://github.com/OWNER/REPO/releases/tag/TAG
```

برای گرفتن لینک صحیح، روی نام فایل ZIP در بخش Assets راست‌کلیک و Copy link address را انتخاب کنید.

## اجرای Workflow

از تب Actions، Workflow زیر را اجرا کنید:

```text
Migrate one annual Release ZIP to Hugging Face
```

ورودی نمونه برای JPMorgan:

```text
release_zip_url: https://github.com/OWNER/REPO/releases/download/TAG/JPMUSUSD_2023.zip
symbol: JPMUSUSD
digits: 3
```

هر ZIP باید دقیقاً یک فایل `.BIN` سالانه با فرمت `bin_v1` پروژه BarReplay داشته باشد. فایل‌های روزانه موجود با همان نام و اندازه دوباره آپلود نمی‌شوند.
