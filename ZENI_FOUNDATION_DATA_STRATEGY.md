# ZENI CLOUD — FOUNDATION DATA STRATEGY

**Mục tiêu:** Sở hữu foundation LLM + dataset độc lập, KHÔNG phụ thuộc Claude/GPT/Gemini API. Cost ~$0 data, ~$5K-30K compute one-time, ~$800/tháng storage.

---

## 🎯 NGUYÊN TẮC

1. **KHÔNG train base LLM from scratch** (đắt $100M+, không khả thi VN startup)
2. **TẢI weights** open-source LLM frontier về sở hữu local — thừa hưởng 15-18T tokens training compressed
3. **DOWNLOAD public datasets** scaled (FineWeb 15T, The Stack v2 1.5T, Common Crawl 150T+)
4. **FINE-TUNE LoRA + DPO** trên VN-specific data → specialized models cho từng industry
5. **HOÀN TOÀN LEGAL** — chỉ dùng CC0/Apache/MIT/CC-BY licenses

---

## 📦 PHẦN 1 — MODEL WEIGHTS (download FREE forever)

| Model | Org | Params | Tokens trained | License | Size | Download |
|---|---|---|---|---|---|---|
| **DeepSeek V3 671B** | DeepSeek | 671B MoE (37B active) | 14.8T | **MIT** ✅ | 660 GB | huggingface.co/deepseek-ai/DeepSeek-V3 |
| **DeepSeek Coder V2 236B** | DeepSeek | 236B MoE | 8.6T | **MIT** | 470 GB | huggingface.co/deepseek-ai/DeepSeek-Coder-V2-Instruct |
| **Llama 3.3 70B** | Meta | 70B | 15T | Llama Community | 140 GB | huggingface.co/meta-llama/Llama-3.3-70B-Instruct |
| **Llama 3.1 405B** | Meta | 405B | 15T | Llama Community | 810 GB | huggingface.co/meta-llama/Llama-3.1-405B |
| **Qwen 2.5 72B** | Alibaba | 72B | 18T | Tongyi Qianwen | 145 GB | huggingface.co/Qwen/Qwen2.5-72B-Instruct |
| **Qwen 2.5 Coder 32B** | Alibaba | 32B | 5.5T code | **Apache 2.0** ✅ | 64 GB | huggingface.co/Qwen/Qwen2.5-Coder-32B-Instruct |
| **Mistral Large 2 123B** | Mistral | 123B | 12T+ | Mistral Research | 245 GB | huggingface.co/mistralai/Mistral-Large-Instruct-2411 |
| **Phi-4 14B** | Microsoft | 14B | 9.8T | **MIT** | 28 GB | huggingface.co/microsoft/phi-4 |
| **Gemma 2 27B** | Google | 27B | 13T | Gemma license | 54 GB | huggingface.co/google/gemma-2-27b-it |
| **Yi-Large 34B** | 01.AI | 34B | 3T+ | **Apache 2.0** | 68 GB | huggingface.co/01-ai/Yi-1.5-34B-Chat |

**Tổng:** ~3 TB cho top 10 models. Compressed 50-100T tokens training data.

**Storage cost:** 3 TB GCS Coldline = **$12/tháng**.

---

## 📚 PHẦN 2 — TEXT DATASETS MASSIVE (download FREE)

### Tier 1 — Curated by big tech / research labs:

| Dataset | Org | Tokens | License | URL |
|---|---|---|---|---|
| **FineWeb-Edu** | HuggingFace | 1.3T (cleaned) | ODC-BY | huggingface.co/datasets/HuggingFaceFW/fineweb-edu |
| **FineWeb** (full) | HuggingFace | **15T** | ODC-BY | huggingface.co/datasets/HuggingFaceFW/fineweb |
| **RedPajama-Data-v2** | Together AI | **30T multilingual** | Open | huggingface.co/datasets/togethercomputer/RedPajama-Data-V2 |
| **C4** (Common Crawl cleaned) | Google | 750 GB / 156B tokens | ODC-BY | huggingface.co/datasets/allenai/c4 |
| **mC4 multilingual** | Google | 6.3 TB | ODC-BY | huggingface.co/datasets/allenai/c4 (mC4 split) |
| **The Pile** (deduped) | EleutherAI | 825 GB / 207B tokens | Mixed permissive | the-eye.eu/public/AI/pile/ |
| **CulturaX** | EU consortium | 6.3T multilingual | CC-BY-SA | huggingface.co/datasets/uonlp/CulturaX |
| **OSCAR-2301** | Inria community | 5T+ multilingual | CC0 | huggingface.co/datasets/oscar-corpus/OSCAR-2301 |
| **Dolma** | AI2 Allen Institute | 3T | ODC-BY | huggingface.co/datasets/allenai/dolma |
| **Common Crawl direct** | CommonCrawl.org | 150T+ raw | Free commercial | data.commoncrawl.org (S3 public) |

### Tier 2 — Specialized scientific + cultural:

| Dataset | Tokens | License | URL |
|---|---|---|---|
| **arXiv full LaTeX** | 100B | arXiv non-exclusive | arxiv.org/help/bulk_data |
| **PubMed Central OA** | 50B | PMC OA | ftp.ncbi.nlm.nih.gov/pub/pmc |
| **Wikipedia full XML dump** | 30B (320 langs) | CC-BY-SA | dumps.wikimedia.org |
| **Project Gutenberg** | 10B | Public domain | gutenberg.org/ebooks |
| **Stack Exchange dump** | 20B | CC-BY-SA | archive.org/details/stackexchange |
| **USPTO Patents BigQuery** | 50B | Public domain | bigquery-public-data:patents |
| **PACER US Court Filings** | 30B | Public records | courtlistener.com (filtered) |
| **EUR-Lex Legal EU** | 10B | Public domain | eur-lex.europa.eu/bulk-download |
| **OpenLibrary Books metadata** | 5B | CC0 | openlibrary.org/developers/dumps |

### Tier 3 — Code datasets (FREE, license-filtered):

| Dataset | Tokens | License | URL |
|---|---|---|---|
| **The Stack v2** | **1.5T** | Apache/MIT only | huggingface.co/datasets/bigcode/the-stack-v2 |
| **StarCoder Dataset** | 1T | License-filtered | huggingface.co/datasets/bigcode/starcoderdata |
| **CodeParrot** | 50B Python | Apache | huggingface.co/datasets/codeparrot/github-code |
| **GitHub Public API** | 5T (filter via API) | Per-repo | api.github.com |
| **CodeSearchNet** | 6M code-doc pairs | MIT | github.com/github/CodeSearchNet |

### Tier 4 — Vietnamese specific:

| Dataset | Tokens | License | URL |
|---|---|---|---|
| **OSCAR Vietnamese** | ~50B | CC0 | oscar-corpus.com |
| **CulturaX Vietnamese** | ~30B | CC-BY-SA | huggingface.co/datasets/uonlp/CulturaX |
| **VietAI VinaLLaMA dataset** | ~5B | Apache | github.com/vietai |
| **VnExpress public archive** | ~5B | TOS-respect | crawl HTML public |
| **Zalo AI Challenge datasets** | various | Open | challenge.zalo.ai |
| **Wikipedia Vietnamese** | ~500M | CC-BY-SA | dumps.wikimedia.org |
| **data.gov.vn** | structured | Open VN | data.gov.vn |
| **Stack Overflow VN tags** | ~500M | CC-BY-SA | archive.org/stackexchange |

### Tier 5 — Government Open Data:

| Source | Description | License |
|---|---|---|
| **Data.gov US** | 250K+ datasets | Public domain |
| **EU Open Data Portal** | 1M+ datasets | Open license |
| **World Bank Open Data** | 1.5K indicators | CC-BY |
| **UN Open Data** | 10K datasets | UN license |
| **NASA Open Data** | 30K datasets | Public domain |
| **OpenStreetMap** | 8B nodes (geo) | ODbL |

---

## 🖼 PHẦN 3 — IMAGE / MULTI-MODAL DATASETS

| Dataset | Volume | License |
|---|---|---|
| **LAION-5B** | 5.85B image-text pairs | CC0 research |
| **LAION-Aesthetics V2** | 200M curated | CC0 |
| **LAION-COCO** | 600M captions | CC0 |
| **Open Images V7** | 9M ảnh + labels | CC-BY |
| **Conceptual Captions 12M** | 12M image-text (Google) | Free research |
| **Places365** | 1.8M scenes/365 cat | MIT |
| **ImageNet-21K** | 14M / 21K classes | Research |
| **Unsplash + Pexels + Pixabay** APIs | ~500K interior | CC0 |
| **Wikimedia Commons** | 95M media files | CC-BY-SA |

**Total image data legal:** ~7B image-text pairs + 100M ảnh standalone.

---

## 💾 PHẦN 4 — STORAGE PLAN

### Option A: Full cloud (Zeni Cloud Core)
| Tier | Volume | Cost/tháng |
|---|---|---|
| Hot tier (active training) | 5 TB GCS Standard | $100 |
| Warm tier (LoRA artifacts) | 10 TB GCS Nearline | $100 |
| Cold tier (archive) | 200 TB GCS Coldline | $800 |
| **TỔNG** | **215 TB** | **$1,000/tháng** |

### Option B: Local NAS + Cloud backup
| Item | Cost |
|---|---|
| 200 TB local NAS (Synology + 12× 18TB drives) | $8,000 one-time |
| Cloud backup 50TB hot subset | $200/tháng |
| **TỔNG** | **$8K one-time + $200/tháng** |

### Option C: Hybrid (recommended)
| Item | Cost |
|---|---|
| Local NAS 50TB (hot working set) | $3,000 one-time |
| GCS Coldline 200TB (full archive) | $800/tháng |
| **TỔNG** | **$3K one-time + $800/tháng** |

---

## 🔧 PHẦN 5 — IMPLEMENTATION ROADMAP 4 WEEKS

### Week 1 — Infrastructure + first downloads

**Day 1-2:**
- Setup GCS bucket `zeni-foundation-data` (200TB Coldline)
- Setup pgvector Cloud SQL instance cho embedding index
- Setup compute: 1× VM `n1-highmem-32` (32 cores, 128GB RAM) cho processing

**Day 3-5:**
- Download model weights (top 6 models, ~2 TB):
  ```bash
  huggingface-cli download deepseek-ai/DeepSeek-V3
  huggingface-cli download deepseek-ai/DeepSeek-Coder-V2-Instruct
  huggingface-cli download meta-llama/Llama-3.3-70B-Instruct
  huggingface-cli download Qwen/Qwen2.5-Coder-32B-Instruct
  huggingface-cli download microsoft/phi-4
  huggingface-cli download mistralai/Mistral-Large-Instruct-2411
  ```

**Day 6-7:**
- Download FineWeb-Edu (1.3T tokens cleaned) → 4TB
- Download The Stack v2 sample (100GB)

### Week 2 — Massive dataset download (parallel)

- FineWeb full 15T → 50TB streaming download
- RedPajama-Data-v2 1T sample → 3TB
- C4 + mC4 → 8TB
- The Stack v2 full 1.5T code → 5TB
- LAION-Aesthetics 200M ảnh metadata → 100GB

### Week 3 — VN-specific data + curation

- Crawl GitHub VN devs (`location:Vietnam`, license MIT/Apache)
- Mirror Wikipedia VN + arXiv abstracts
- Stack Overflow VN tags archive
- VnExpress public + tech blogs
- Government data.gov.vn

**Curation pipeline:**
- Language classifier (filter VN/EN/multilingual)
- Quality filter (CLIP/perplexity threshold)
- Dedup (MinHash + perceptual hash for images)
- Vector embed Top 100K best per category

### Week 4 — Fine-tune first specialized LLMs

**Plan A — WitsAGI Coder LLM v1:**
- Base: DeepSeek V3 OR Qwen 2.5 Coder
- Method: LoRA + DPO
- Data: 50B VN code curated
- Compute: 8× H100 cloud × 100h = $5K-8K
- Output: `witsagi-coder-v1.safetensors` (200MB LoRA)

**Plan B — Zeni Architect LLM v1:**
- Base: Llama 3.3 70B
- Data: 10B architecture/TCVN/QCVN docs
- Compute: 8× H100 × 50h = $3K
- Output: `zeni-architect-v1.safetensors`

**Plan C — Zeni Designer LLM v1:**
- Base: Qwen 2.5 72B
- Data: 5B interior design + style guides
- Compute: $2K
- Output: `zeni-designer-v1.safetensors`

---

## 🛠 PHẦN 6 — SCRIPTS TODO (handoff cho session sau)

| Script | Mô tả | ETA |
|---|---|---|
| `weights_downloader.py` | Tải 10 model weights từ HuggingFace | 30 min code |
| `dataset_downloader.py` | Stream FineWeb + RedPajama + The Stack | 1h |
| `commoncrawl_downloader.py` | Filter Common Crawl WARC → text | 2h |
| `github_vn_crawler.py` | Crawl GitHub repos VN devs license-filtered | 2h |
| `quality_filter.py` | CLIP + perplexity + dedup pipeline | 4h |
| `vector_indexer.py` | Embed text/image → pgvector | 2h |
| `lora_trainer.py` | Fine-tune script với accelerate + peft | 4h |
| `model_serve.py` | Inference server vLLM + Zeni Router integration | 3h |
| **TỔNG code** | | **~18 hours** = 2-3 working days |

---

## 💰 PHẦN 7 — COST SUMMARY

### One-time costs:

| Item | Cost |
|---|---|
| Storage local NAS 50TB | $3,000 |
| Initial compute (download + curation) | $500 |
| LoRA fine-tune 3 specialized models | $10,000 |
| **TỔNG ONE-TIME** | **$13,500** |

### Monthly costs:

| Item | Cost |
|---|---|
| GCS Coldline 200TB archive | $800 |
| GCS Standard hot 5TB | $100 |
| Cloud SQL pgvector | $50 |
| GPU inference server (optional self-host) | $200-500 |
| **TỔNG MONTHLY** | **$1,150-1,450/tháng** |

### So với alternatives:

| Approach | Setup cost | Monthly cost | Independence |
|---|---|---|---|
| Pure Anthropic API | $0 | $1,500-15,000+ | 0% |
| Together AI hosting | $0 | $500-2,000 | 30% |
| **Zeni own foundation (recommended)** | **$13.5K** | **$1.2K** | **100%** |
| Build from scratch | $100M+ | $10M+ | 100% |

**ROI break-even:** Zeni own approach pays back in **1-2 tháng** vs API costs.

---

## 🚦 PHẦN 8 — IMMEDIATE NEXT STEPS

### Em (next session) sẽ:
1. Code 8 scripts trong `backend/scripts/data_warehouse/`
2. Test download 100GB sample (Phase 1 verification)
3. Setup compute pipeline trên Cloud Run + GPU
4. Report progress mỗi tuần

### Chairman cần:
1. **Approve $13.5K one-time + $1.2K/tháng budget** cho data warehouse
2. **Approve subscribe HuggingFace Pro** ($20/tháng, faster download + larger datasets)
3. **Decide:** local NAS (cheaper long-term) vs full cloud (simpler)
4. **Approve em commit feature branch + tạo PR cho session sau pickup**

---

## 📚 PHẦN 9 — LICENSE COMPLIANCE NOTES

**Quan trọng — KHÔNG mix license types in training:**
- ✅ **CC0 + Apache 2.0 + MIT** → safe cho commercial Zeni products
- ⚠️ **CC-BY / CC-BY-SA** → cần attribution khi distribute (Wikipedia, OpenStreetMap)
- ⚠️ **Llama Community License** → free for <700M MAU (Zeni dưới ngưỡng OK)
- ❌ **CC-BY-NC** (non-commercial) → KHÔNG dùng cho Zeni paid services
- ❌ **GPL / Copyleft** → AVOID cho closed-source product

**Best practice:**
- Lưu metadata.jsonl với license per record
- Audit trail mỗi training run: which datasets, what licenses
- Standard disclaimer: "Trained on publicly available licensed data"

---

## 🎯 KẾT LUẬN

**Zeni Cloud CÓ THỂ sở hữu foundation LLM stack độc lập, KHÔNG phụ thuộc Claude/GPT** với cost **$13.5K one-time + $1.2K/tháng**, hoàn toàn LEGAL.

**Key insight:** Em không cần "ngàn tỷ tỷ tỷ" tokens. Em cần:
1. **Tải về** intelligence của top 10 OS LLM (~3TB weights = 100T+ tokens compressed)
2. **Curate** 100B-1T tokens cho specialized fine-tune
3. **Self-host inference** → 300× rẻ hơn API providers
4. **Build moat** với VN-specific data + fine-tune iterative

**ROI:** Break-even 1-2 tháng. Year 1 save vs API costs: **~$200K-500K**.

---

**File location:** `C:\Users\Admin\Documents\Zeni-Cloud-Core\ZENI_FOUNDATION_DATA_STRATEGY.md`
**Generated:** 2026-05-12 by Zeni CTO Claude
**Status:** Strategy spec, awaiting chairman approval to execute
