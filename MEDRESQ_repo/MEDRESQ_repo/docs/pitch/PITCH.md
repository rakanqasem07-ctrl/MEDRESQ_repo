# MEDRESQ pitch (4 minutes)

Seven slides for the QRCE competition, Global Entrepreneurship Week 2026, 11 November 2026.
The slide deck itself is not in this repository; this file keeps the slide text and the speaking script (Jordanian Arabic).
Numbers marked * are hypotheses or simulation outputs on synthetic data. Nothing here is a real-hospital result.

| # | Slide | Time | Rubric items covered |
|---|---|---|---|
| 1 | Cover | 5 s | First impression |
| 2 | Problem and first customer | 25 s | Segment, problem |
| 3 | Solution and validation | 35 s | Product sophistication |
| 4 | Market, competition, protection, IP | 40 s | Market growth, competitive landscape, protection, IP |
| 5 | Business model and revenue | 65 s | Business model, revenue stream, sustainable revenue |
| 6 | Environmental impact | 40 s | Environmental impact at scale |
| 7 | Team and ask | 30 s | Team |

## 1. Cover
**MEDRESQ.** Medicine is not missing. It is in the wrong pharmacy. We move it before it runs out or expires.

> السلام عليكم. احنا فريق [اسم الفريق]، ومشروعنا MEDRESQ: الدواء مش ناقص، هو بس بالصيدلية الغلط.

## 2. One pharmacy runs out. Another one's stock expires.
Pharmacy A: 2 packs left of a heart medicine, next delivery in 7 days. Hospital B: more than it needs, part will expire unused.
First customer: pharmacy managers of hospitals and multi-branch groups in Amman who move stock by phone today. They buy after a stockout of a critical medicine or an expired batch. We have spoken with 7 pharmacies.

> تخيّلوا صيدلية عندها عبوتين بس من دواء قلب، والشحنة بعد 7 أيام. ومستشفى قريب عنده أكتر من حاجته وجزء منه رح تنتهي صلاحيته. المشكلة مش إنو الدواء مش موجود، المشكلة إنو ما حدا شايف الصورة كاملة. زبوننا الأول: مديرو صيدليات المستشفيات والمجموعات متعددة الفروع بعمّان، اللي بينقلوا المخزون بالتلفون، وبيقرروا يشتروا بعد نفاد دواء حرج أو دفعة منتهية. حكينا مع 7 صيدليات.

## 3. MEDRESQ predicts, suggests, and a pharmacist approves
1. Predict: forecast per medicine, reacts to demand surges.
2. Suggest: cheapest transfer; the donor keeps 21 days of stock.
3. Approve: both pharmacists confirm; every step is logged.

Simulation on synthetic data, 100 runs per scenario, assumed prices: total cost 19 to 24% lower, stockout days 15 to 21% fewer, expiry loss 48 to 59% lower. Not a real-hospital result. See `prototype/RESULTS.md`.

> المدخلات: مخزون ومبيعات وتواريخ صلاحية كل مركز. المعالجة: بنتوقع أي دواء رح ينفد ومتى، وبنحسب أرخص نقل بين المراكز، والمركز المانح بيحتفظ بمخزون 21 يوم والدفعة لازم يكون فيها 30 يوم صلاحية على الأقل. المخرج: اقتراح نقل، والصيدلانيين من الطرفين بيوافقوا، وكل خطوة بتتسجل. التحقق: محاكاة 100 مرة لكل سيناريو. بصراحة: أرقام اصطناعية، مش نتيجة من مستشفى حقيقي، وعندنا ديمو حي إذا بدكم.

## 4. Alternatives see one site. We see the network.
Alternatives today: phone and chat calls (the real competitor), pharmacy inventory software (one site only), supplier ordering (buys new stock, does not move existing stock).
Why they cannot copy us quickly: network effect, switching cost. IP status: none registered. Planned: software copyright and the MEDRESQ trademark.
Market: about 5,200 pharmacies and 121 hospitals in Jordan. If every site paid the assumed price, about JD 1.7M a year (upper bound, not a forecast). Start with an 8-center network in Amman.
Counts from the Pharmacists Association (undated) and the US Commercial Service (Feb 2026).

> السوق والمنافسة. بالأردن حوالي 5,200 صيدلية و121 مستشفى. لو كل موقع دفع سعرنا المفترض، هاد حوالي 1.7 مليون دينار بالسنة، وهاد سقف أعلى مش توقع. المنافس الحقيقي هو التلفون والرسائل. برامج المخزون بتتابع موقع واحد، ومنصات الطلب بتشتري مخزون جديد وما بتحرّك الموجود. احنا بنشوف الشبكة. بنحمي حالنا بتأثير الشبكة وكلفة الانتقال. وبالنسبة للملكية الفكرية: لسا ما سجّلنا شي. المخطط: حقوق المؤلف للبرمجيات والعلامة التجارية MEDRESQ.

## 5. Business model and revenue
Monthly subscription per center. Price*: JD 25 per pharmacy, JD 100 per hospital. Contribution: about JD 19 per center per month (about 53%). Break-even: 4 centers for servers, 20 with a small team stipend.
Why they pay: in the simulation the fee is about 7% of the saving. How we grow: paid pilot, then chains and hospital groups on a network plan.
*Hypotheses, not yet tested with pharmacists. Costs are estimates (`docs/business/MEDRESQ_Cost-Sheet.xlsx`).

> الزبون الصيدلية أو المستشفى، والدفع اشتراك شهري لكل مركز. السعر المفترض: 25 دينار للصيدلية و100 للمستشفى بالشهر. كلفة خدمة المركز الواحد حوالي 17 دينار، فالمساهمة حوالي 19 دينار، هامش حوالي 53 بالمية. نقطة التعادل: 4 مراكز تغطي السيرفرات، و20 مركز تغطي مكافأة بسيطة للفريق. وبصراحة: الأسعار فرضيات لسا ما اختبرناها مع الصيادلة.

## 6. Less medicine thrown away, measured in packs
Baseline about 15 packs expire per center per year; with MEDRESQ about 8 (about 7 fewer). At 150 centers, about 1,000 packs a year (assumption). Trade-off: transfers move packs about 28 km on average. Kilograms and emissions are not measured yet; the pilot will record them.

> المؤشر: عبوات الدواء اللي بتنتهي صلاحيتها بدون استخدام. بالمحاكاة بدوننا حوالي 15 عبوة لكل مركز بالسنة، ومعنا حوالي 8. لو وصلنا 150 مركز، حوالي ألف عبوة بالسنة. وبصراحة، في مقايضة: النقل بيحرّك العبوات حوالي 28 كم بالمتوسط، وما حوّلناه لانبعاثات لسا.

## 7. The team, and what we ask
Rakan Qasem (Data Science and AI, built the prototype and AI model), Noor Shuquara (idea originator, data collection), Omar Momani (hardware prototype).
Validation so far: spoke with 7 pharmacies; hospital requests sent, awaiting reply; next, a written question to the JFDA on transfers.
We ask for: introductions to hospital pharmacy managers, guidance on transfer rules between centers, a partner for a read-only pilot.
