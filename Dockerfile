FROM python:3.10-slim

# FFmpeg এবং অন্যান্য প্রয়োজনীয় টুলস ইন্সটল করা হচ্ছে
RUN apt-get update && apt-get install -y ffmpeg

# ওয়ার্কিং ডিরেক্টরি সেট করা
WORKDIR /app

# রিকোয়ারমেন্টস কপি করে পাইথন প্যাকেজ ইন্সটল করা
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# প্রজেক্টের বাকি সব ফাইল কপি করা
COPY . .

# আপনার বট স্টার্ট করার কমান্ড (যদি আপনার ফাইলের নাম main.py হয়)
# ফাইলের নাম অন্য কিছু হলে main.py এর জায়গায় সেই নাম দিন, যেমন: app.py বা bot.py
CMD ["python", "main.py"]
