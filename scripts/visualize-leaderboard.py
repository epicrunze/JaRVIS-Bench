#!/usr/bin/env python3
"""Generate a leaderboard bar chart comparing JaRVIS results against NL2Repo-Bench."""

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
from matplotlib.patches import Patch
from pathlib import Path

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["DejaVu Sans", "Helvetica", "Arial"],
    "axes.unicode_minus": False,
})

# --- Data ---

# NL2Repo-Bench published scores (from leaderboard figure)
leaderboard = [
    ("Claude-Sonnet-4.5\n(ClaudeCode)", 40.2),
    ("Claude-Sonnet-4.5\n(Cursor)", 39.9),
    ("Claude-Sonnet-4.5\n(Openhands)", 39.2),
    ("Claude-Sonnet-4\n(Openhands)", 37.0),
    ("Gemini-3-pro\n(Openhands)", 34.2),
    ("DeepSeek-V3.2\n(Openhands)", 27.6),
    ("Kimi-K2\n(Openhands)", 22.7),
    ("DeepSeek-V3.1\n(Openhands)", 22.2),
    ("GPT-5\n(Openhands)", 21.7),
    ("Qwen3-Instruct\n(Openhands)", 17.9),
    ("GLM-4.6\n(Openhands)", 17.5),
    ("Qwen3-T\n(Openhands)", 13.8),
]

# Our results (clean runs only, batch_20260318-033128_1f0e)
our_results = [
    ("Claude-Sonnet-4.6\n+ JaRVIS (ClaudeCode)", 49.2),
    ("Claude-Sonnet-4.6\n(ClaudeCode) [ours]", 46.1),
]

# Combine and sort descending
all_entries = our_results + leaderboard
all_entries.sort(key=lambda x: x[1], reverse=True)

names = [e[0] for e in all_entries]
scores = [e[1] for e in all_entries]

# --- Colors ---

# Match the NL2Repo-Bench palette: muted blues that fade lighter for lower scores
jarvis_color = "#D4A04A"   # muted gold
baseline_color = "#B8A0D6" # soft lavender
lb_palette = [
    "#3B5998", "#4A6BA8", "#5A7DB8", "#6990C5",
    "#79A3D2", "#8BB5DC", "#9CC5E4", "#ABCFE8",
    "#B8D8EC", "#C5E0F0", "#D2E8F4", "#DFEFF8",
]

# Map colors to sorted entries
lb_idx = 0
colors = []
for name, _ in all_entries:
    if "+ JaRVIS" in name:
        colors.append(jarvis_color)
    elif "[ours]" in name:
        colors.append(baseline_color)
    else:
        colors.append(lb_palette[lb_idx])
        lb_idx += 1

# --- Plot ---

fig, ax = plt.subplots(figsize=(15, 6.5))
fig.set_facecolor("white")
ax.set_facecolor("white")

x = np.arange(len(names))
bar_width = 0.78

bars = ax.bar(x, scores, width=bar_width, color=colors, edgecolor="white", linewidth=0.8)

# Score labels on top of each bar
for bar, score in zip(bars, scores):
    ax.text(
        bar.get_x() + bar.get_width() / 2,
        bar.get_height() + 0.6,
        f"{score:.1f}",
        ha="center",
        va="bottom",
        fontsize=9.5,
        fontweight="semibold",
        color="#333333",
    )

ax.set_xticks(x)
ax.set_xticklabels(names, rotation=40, ha="right", fontsize=8.5, color="#333333")
ax.set_ylabel("Average Score", fontsize=11, color="#333333", labelpad=8)

# Y-axis styling
ax.set_ylim(0, max(scores) + 5)
ax.yaxis.set_major_locator(mticker.MultipleLocator(10))
ax.tick_params(axis="y", labelsize=9, colors="#555555")
ax.tick_params(axis="x", length=0)  # no x tick marks

# Minimal spines — only left and bottom
ax.spines["top"].set_visible(False)
ax.spines["right"].set_visible(False)
ax.spines["left"].set_color("#CCCCCC")
ax.spines["left"].set_linewidth(0.6)
ax.spines["bottom"].set_color("#CCCCCC")
ax.spines["bottom"].set_linewidth(0.6)

# Subtle horizontal grid
ax.yaxis.grid(True, color="#E8E8E8", linewidth=0.5, linestyle="-")
ax.set_axisbelow(True)

# Legend — top right, clean box
legend_elements = [
    Patch(facecolor=jarvis_color, edgecolor="white", label="JaRVIS (this work)"),
    Patch(facecolor=baseline_color, edgecolor="white", label="Baseline (this work)"),
    Patch(facecolor=lb_palette[2], edgecolor="white", label="NL2Repo-Bench"),
]
legend = ax.legend(
    handles=legend_elements,
    loc="upper right",
    fontsize=9,
    frameon=True,
    fancybox=False,
    edgecolor="#CCCCCC",
    framealpha=1.0,
    title="Agent Framework",
    title_fontsize=9,
)
legend.get_frame().set_linewidth(0.6)

# Footnote
fig.text(
    0.5, 0.005,
    "† This work uses clean runs only (n=90 paired tasks). "
    "NL2Repo-Bench scores from published leaderboard.",
    ha="center", fontsize=7.5, fontstyle="italic", color="#999999",
)

plt.tight_layout()
plt.subplots_adjust(bottom=0.20)

# Save
out_dir = Path(__file__).resolve().parent.parent / "analysis"
out_dir.mkdir(exist_ok=True)

out_path = out_dir / "leaderboard-comparison.png"
fig.savefig(out_path, dpi=200, bbox_inches="tight", facecolor="white")
print(f"Saved to {out_path}")

out_pdf = out_dir / "leaderboard-comparison.pdf"
fig.savefig(out_pdf, bbox_inches="tight", facecolor="white")
print(f"Saved to {out_pdf}")
