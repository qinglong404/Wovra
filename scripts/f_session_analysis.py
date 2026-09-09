"""F 组会话（DeepSeek 首战）逐调用分析：命中率曲线 × 前缀体量、org 触发、TTFT。"""
import json
import re

p = 'tasks/20260909-125004-c0f4a5/task.json'
d = json.load(open(p))
print("goal:", (d.get('goal') or '')[:60])
print("rounds:", len(d['rounds']), "| org_state:", [r.get('org_state') for r in d['rounds']])
print("steps_used:", [r.get('steps_used') for r in d['rounds']])

rows = [h['detail'] for h in d['history'] if h['kind'] == 'llm_call']
print(f"\nllm_call 行数: {len(rows)}")
purposes = {}
total_prompt = total_hit = total_miss = 0
curve = []
for r in rows:
    pm = re.search(r'prompt=([\d,]+)', r)
    cm = re.search(r'cached=([\d,]+)', r)
    mm = re.search(r'miss=([\d,]+)', r)
    pp = re.search(r'\[(\w+)\]', r)
    tt = re.search(r'ttft=([\d.]+)s', r)
    prompt = int(pm.group(1).replace(',', '')) if pm else 0
    cached = int(cm.group(1).replace(',', '')) if cm else 0
    miss = int(mm.group(1).replace(',', '')) if mm else 0
    purpose = pp.group(1) if pp else '?'
    ttft = float(tt.group(1)) if tt else 0
    purposes[purpose] = purposes.get(purpose, 0) + 1
    total_prompt += prompt
    total_hit += cached
    total_miss += miss
    curve.append((prompt, cached, miss, ttft, purpose))

print("purpose 分布:", purposes)
print(f"Σ prompt={total_prompt:,} Σ hit={total_hit:,} Σ miss={total_miss:,}")
print(f"命中率: {total_hit / total_prompt:.2%}")

buckets = {}
for prompt, cached, miss, ttft, purpose in curve:
    if purpose != 'working' or prompt < 1000:
        continue
    b = min(prompt // 50000 * 5, 30)
    key = f"{b}-{b + 5}万"
    agg = buckets.setdefault(key, [0, 0])
    agg[0] += cached
    agg[1] += prompt
print("\n工作调用命中率 × 前缀体量分桶：")
for k in sorted(buckets, key=lambda x: int(x.split('-')[0])):
    c, p2 = buckets[k]
    print(f"  前缀 {k} tok: 命中 {c / p2:.1%}（{p2:,} tok）")

ttfts = [t for prompt, cached, miss, t, purpose in curve if purpose == 'working' and prompt > 100000]
if ttfts:
    print(f"\n>10 万 tok 大前缀调用 {len(ttfts)} 次：TTFT 均值 {sum(ttfts) / len(ttfts):.1f}s 最大 {max(ttfts):.1f}s")
else:
    print("\n无 >10 万 tok 的工作调用")
