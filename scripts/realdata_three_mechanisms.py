"""真实轨迹 × 三种机制：同一组实测基座曲线/步数，三种上下文策略的账目对照。

数据来源：用户提供的一条真实生产会话（29 轮 / 606 步 / 基座 0→726.3K /
命中 256.9M / 未命中 3.5M / 输出 444.7K）。

机制：
  baseline      全量回放（实测发生的——这条曲线就是它的真实上下文轨迹）
  managed-cur   现行 managed：降档渲染 + 近 3 轮全量 + 尾部 + 逐轮整理
  managed-v3    V3 设计：同上装配，但整理改为水位（100K）触发的批量合并

校准：baseline 的 Σ 步数×基座 按实测计费（260.4M）归一，同一系数
应用到 managed 曲线。整理成本按实测观察值校准（约 4-6K/次）。
"""

HIT, MISS, OUT = 0.115, 0.4, 1.4   # GLM-5.3-flash 半价 元/M
DEMOTE_RATIO = 0.1                  # 已整理轮的渲染压缩比（精修索引 ≈ 原文 1/10）
TAIL = 15_000                       # 状态 + 任务索引 + 文件地图（逐轮重写）
ORG_FIXED = 4_000                   # 整理调用固定开销（提示词 + JSON 输出）
ORG_PER_ROUND = 1_000               # 批内每轮的索引展开
WATERMARK = 100_000                 # V3 水位（未整理内容触发批量整理）

BASES_K = [0, 98.9, 182.9, 236.7, 268, 269.1, 299.9, 304.5, 353.9, 369.6,
           371.1, 388.1, 414.4, 430.3, 464.2, 481.3, 510.6, 527.7, 553,
           558.4, 586.3, 589.6, 617.3, 640, 657.4, 680.5, 705.7, 711.9, 726.3]
STEPS = [29, 63, 44, 22, 1, 26, 2, 40, 16, 1, 5, 21, 15, 24, 16, 27, 22,
         27, 9, 34, 7, 34, 25, 15, 23, 27, 6, 17, 8]


def main() -> None:
    n = len(BASES_K)
    bases = [b * 1000 for b in BASES_K]
    deltas = [bases[i + 1] - bases[i] for i in range(n - 1)] + [14_400]

    # 实测校准：baseline 的模型计费 ÷ 实际计费
    model_bill = sum(s * b for s, b in zip(STEPS, bases))
    actual_bill = 260.4e6
    calib = actual_bill / model_bill

    def simulate(mode: str):
        rows = []
        cum_input = cum_eff = cum_yuan = 0.0
        fresh = 0.0
        org_events = 0
        prev_ctx = 0.0
        for k in range(n):
            s, delta = STEPS[k], deltas[k]
            if mode == "baseline":
                ctx = bases[k] + delta / 2                     # 轮中均值
                hit_share = 256.9e6 / 260.4e6                  # 实测命中占比
            else:
                recent = sum(deltas[max(0, k - 2):k + 1])
                demoted_mass = bases[k] - recent if k >= 3 else 0
                demoted_render = demoted_mass * DEMOTE_RATIO
                if mode == "cur":
                    demoted_render *= 0.5   # 现行 tier 渲染更轻（实测校准）
                ctx = demoted_render + recent + TAIL + delta / 2
                churn = TAIL if mode == "v3" else TAIL * 0.6
                hit_share = max(0.0, 1 - (churn + delta / 2) / max(ctx, 1))
            billing = s * ctx * calib
            cum_input += billing
            hit_tok = billing * hit_share
            miss_tok = billing * (1 - hit_share)
            eff = miss_tok + hit_tok * (0.115 / 0.4)
            cum_eff += eff
            cum_yuan += (hit_tok * HIT + miss_tok * MISS) / 1e6
            # 整理成本（managed 系列）
            if mode != "baseline":
                if mode == "cur":
                    org = ORG_FIXED + ORG_PER_ROUND
                else:
                    fresh += delta
                    if fresh >= WATERMARK:
                        org = ORG_FIXED + fresh * (ORG_PER_ROUND / 25_000)
                        fresh = 0.0
                        org_events += 1
                    else:
                        org = 0.0
                if org:
                    cum_yuan += org / 1e6 * MISS  # 整理调用按未命中价计
                    cum_eff += org
                    cum_input += org
            rows.append((k + 1, s, delta, ctx, cum_input, cum_eff, cum_yuan))
        return rows, cum_input, cum_eff, cum_yuan, org_events

    results = {m: simulate(m) for m in ("baseline", "cur", "v3")}

    print("逐轮对照（每 4 轮采样）:")
    print(f"{'轮':>3} {'步':>3} {'净增K':>7} | {'基座K':>7} {'现行ctx':>8} {'v3 ctx':>8} | {'现行¥':>7} {'v3¥':>7}")
    for i in range(0, n, 4):
        b_row = results["baseline"][0][i]
        c_row = results["cur"][0][i]
        v_row = results["v3"][0][i]
        print(f"R{i+1:<2} {STEPS[i]:>3} {b_row[2]/1000:>7.1f} | {BASES_K[i]:>7.1f} "
              f"{c_row[3]/1000:>8.0f} {v_row[3]/1000:>8.0f} | "
              f"{c_row[6]:>7.2f} {v_row[6]:>7.2f}")
    print()
    print("=== 29 轮总账（GLM 半价）===")
    for m, label in (("baseline", "baseline 对照"), ("cur", "managed 现行"), ("v3", "managed V3水位")):
        _, ci, ce, cy, oe = results[m]
        print(f"  {label:<16} 输入计费 {ci/1e6:>7.1f}M | 等效 {ce/1e6:>6.2f}M | "
              f"现金 {cy:>6.2f} 元 | 整理触发 {oe if m == 'v3' else (n if m == 'cur' else 0)} 次")
    b_total = results["baseline"][2]
    print()
    print(f"等效成本 对照/实验 = {results['baseline'][2] / results['cur'][2]:.2f}x（现行）"
          f" / {results['baseline'][2] / results['v3'][2]:.2f}x（V3 水位）")


if __name__ == "__main__":
    main()
