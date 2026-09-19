# 🛠️ Star Assistant Tools — Developer Guide & Modular Structure

> **বাংলা গাইড ও আর্কিটেকচার নির্দেশিকা**
> এই ডিরেক্টরিতে Star Assistant-এর সমস্ত টুলস ক্যাটাগরি অনুযায়ী সাজানো রয়েছে। আপনি যাতে খুব সহজে বুঝতে পারেন এবং ভবিষ্যতে নতুন যেকোনো টুল যোগ করতে পারেন, তার জন্য এটি মডুলার করা হয়েছে।

---

## 📂 ডিরেক্টরি কাঠামো (Directory Structure)

```
Backend/tools/
├── __init__.py                # ⚡ Auto-Discovery Loader (স্বয়ংক্রিয়ভাবে সব টুল রেজিস্টার করে)
├── registry.py                # Core Engine (@register_tool ডেকোরেটর ও এক্সিকিউটর)
├── README.md                  # এই নির্দেশিকা ফাইল
│
├── youtube/                   # 🎬 ইউটিউবের সম্পূর্ণ প্যাকেজ (Dedicated YouTube Folder)
│   ├── control.py             # প্লে, পজ, নেক্সট, প্রিভিয়াস, স্পিড, HD, ফুলস্ক্রিন, মিউট, সিক
│   ├── history.py             # ওয়াচ হিস্ট্রি সংরক্ষণ ও আগের ভিডিও রিপ্লে
│   ├── analytics.py           # ট্রেন্ডিং ভিডিও ও মিউজিক বিশ্লেষণ
│   └── search.py              # ইউটিউব ভিডিও সার্চ ও সরাসরি গান বাজানো
│
├── system/                    # 🖥️ হার্ডওয়্যার ও উইন্ডোজ সিস্টেম কন্ট্রোল
│   ├── volume.py              # ভলিউম বাড়ানো/কমানো, সেট করা, মিউট করা
│   ├── brightness.py          # স্ক্রিন ব্রাইটনেস নিয়ন্ত্রণ
│   └── telemetry.py           # ব্যাটারি, সিপিইউ, র‍্যাম, পিসি লক, ফোল্ডার ওপেন
│
├── apps/                      # 🚀 অ্যাপ্লিকেশন ও সফটওয়্যার কন্ট্রোল
│   └── launcher.py            # সফটওয়্যার/ওয়েবসাইট খোলা ও বন্ধ করা
│
├── agent/                     # 🤖 অটোনোমাস এজেন্ট, ভিশন ও মেমরি
│   ├── autonomous.py          # অটোনোমাস মাল্টি-স্টেপ কম্পিউটার স্কিল ও রিঅ্যাক্ট লুপ
│   ├── vision.py              # স্ক্রিনশট নেওয়া ও OCR দিয়ে স্ক্রিন রিড করা
│   └── memory.py              # লং-টার্ম ভেক্টর মেমরি ও ফ্যাক্ট মনে রাখা
│
└── utilities/                 # 🧮 ক্যালকুলেশন ও সাধারণ টুলস
    └── calculator.py          # বাংলা ও ইংরেজি সংখ্যার গাণিতিক হিসাব-নিকাশ
```

---

## ⚡ অটোমেটিক টুল ডিসকভারি (Auto-Discovery Feature)

**আপনার কোনো কনফিগারেশন ফাইল এডিট করতে হবে না!**
`Backend/tools/__init__.py`-এ একটি শক্তিশালী অটো-লোডার যুক্ত করা হয়েছে। 
আপনি যখনই:
1. যেকোনো ক্যাটাগরি ফোল্ডারে (যেমন `system/`, `apps/`, `media/`, `agent/`, `utilities/`) নতুন কোনো `.py` ফাইল বানাবেন,
2. অথবা সম্পূর্ণ নতুন কোনো ক্যাটাগরি ফোল্ডার বানিয়ে তার ভেতরে `.py` ফাইল রাখবেন,
3. এবং ফাংশনের ওপরে `@register_tool` ডেকোরেটর দেবেন—

Star Assistant চালু হওয়ার সাথে সাথেই সেই টুলটি **অটোমেটিক রেজিস্টার** হয়ে যাবে!

---

## 📝 নতুন টুল যোগ করার নিয়ম (How to Add a New Tool)

### ধাপ ১: সঠিক ক্যাটাগরি ফোল্ডারে একটি `.py` ফাইল খুলুন
উদাহরণস্বরূপ, আপনি যদি একটি নতুন ওয়েদার বা নোটিফিকেশন টুল যোগ করতে চান, আপনি `system/` বা `utilities/` ফোল্ডারে একটি ফাইল বানাতে পারেন (যেমন: `utilities/weather.py`)।

### ধাপ ২: নিচের টেমপ্লেট অনুযায়ী কোড লিখুন

```python
"""
My Custom Tool description.
"""
from typing import Dict, Any
# Tools ডিরেক্টরি থেকে registry import করুন:
from ..registry import register_tool


@register_tool(
    name="my_new_tool",
    description="এই টুলটি কী কাজ করে তা ইংরেজিতে পরিষ্কার করে লিখুন।"
)
def my_new_tool(param1: str, count: int = 1) -> Dict[str, Any]:
    """আপনার টুলের পাইথন লজিক এখানে লিখুন."""
    try:
        # আপনার কাজ করুন...
        result = f"Hello {param1}, count is {count}"
        return {"success": True, "data": result}
    except Exception as e:
        return {"success": False, "error": str(e)}
```

### ধাপ ৩: কমান্ড রাউটারে যুক্ত করা (Command Router)
ব্যবহারকারী মুখে বা টাইপ করে কী বললে এই টুলটি কাজ করবে, তা নির্ধারণ করতে `Backend/nlu/command_router.py`-তে গিয়ে আপনার টুলের নাম দিয়ে কল করুন:

```python
from ..tools.registry import execute_tool

res = execute_tool("my_new_tool", param1="example", count=5)
```

---

## 🔍 টুল রেজিস্টার হয়েছে কিনা পরীক্ষা করবেন কীভাবে? (Verification)

টার্মিনালে এই কমান্ডটি চালালেই সব রেজিস্টার্ড টুলের তালিকা দেখতে পাবেন:

```bash
python -c "import Backend.tools; print(f'Total tools: {len(Backend.tools.get_all_tools())}'); print(list(Backend.tools.get_all_tools().keys()))"
```
