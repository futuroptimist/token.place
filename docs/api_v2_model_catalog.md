# token.place API v2 model catalogue

The table below records the models now exposed by `/api/v2/models`, along with the
quantised artifact chosen to ensure each option can run on a single RTX 4090 (24 GB)
using publicly documented builds. File sizes or vendor statements under 24 GB provide
the headroom check.

| Model ID | Notes on deployability | Reference |
| --- | --- | --- |
| `llama-3-8b-instruct` | Q4_K_M GGUF is ~4.92 GB, well under the 24 GB VRAM budget. | `Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf` entry.【9f6427†L1-L2】 |
| `gpt-oss-20b` | Official model card notes the 20B MoE variant runs within 16 GB. | `unsloth/gpt-oss-20b-GGUF` README.【6d129f†L1-L2】 |
| `mistral-7b-instruct` | Recommended Q4_K_M build consumes ~6.87 GB of RAM when fully offloaded. | TheBloke GGUF table.【445fd2†L1-L2】 |
| `mixtral-8x7b-instruct` | Q3_K_M mixtral file is ~20.36 GB with a 22.86 GB RAM requirement, fitting a 24 GB card. | TheBloke GGUF table.【6569fa†L1-L2】 |
| `phi-3-mini-4k-instruct` | Q4_K_M weight is ~2.39 GB, leaving ample VRAM. | bartowski GGUF table.【c04343†L1-L2】 |
| `mistral-nemo-instruct` | Q4_K_M quant weighs ~7.48 GB, suitable for consumer GPUs. | bartowski GGUF table.【f49f13†L1-L2】 |
| `qwen2.5-7b-instruct` | Q4_K_M quant is 4.68 GB, providing plenty of margin. | bartowski GGUF table.【d41531†L1-L1】 |
| `qwen2.5-coder-7b-instruct` | Code-tuned Q4_K_M variant is also 4.68 GB. | bartowski GGUF table.【0e302d†L1-L1】 |
| `gemma-2-9b-it` | Q4_K_M chat weights are ~5.76 GB. | bartowski GGUF table.【22fcc6†L1-L1】 |
| `codegemma-7b` | Coding GGUF weighs ~5.32 GB. | bartowski GGUF table.【c061cc†L1-L1】 |
| `smollm2-1.7b-instruct` | Lightweight GGUF is about 1.06 GB. | bartowski GGUF table.【ca65e4†L1-L1】 |

These measurements stay within RTX 4090 limits while covering general chat, coding,
and compact helper workloads. Future additions should update this file with the
supporting citation before advertising new models.

The safety-tuned adapter `llama-3-8b-instruct:alignment` reuses the same
Meta Llama 3.1 quantised artifact as the base `llama-3-8b-instruct` entry, so no
additional GPU budget is required beyond the 4.9 GB captured in the table.
