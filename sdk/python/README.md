# zenicloud · Python SDK

Official Python SDK for [Zeni Cloud](https://zenicloud.io).

## Install

```bash
pip install zenicloud
```

## Quick start

```python
import os
from zenicloud import ZeniCloud

zeni = ZeniCloud(token=os.environ["ZENI_TOKEN"], workspace="nexbuild")

# 1. Architecture agent
result = zeni.agents.architecture.run(
    brief="Biet thu 3 tang Phu My Hung 200m2 Tropical Modern",
    generate_renders=True, n_renders=2,
    constraints={"area_m2": 200, "floors": 3, "budget_vnd": 8_000_000_000},
)
print(result["concept"][:500])
print(f"Renders: {len(result['renders'])} ảnh, cost ${result['cost_usd']}")

# 2. Image generation
img = zeni.ai.generate_image(prompt="Tropical villa, golden hour, 8K", n=1)
data_uri = img["images"][0]["data_uri"]

# 3. Streaming
for chunk in zeni.ai.stream(prompt="Liệt kê 5 phong cách 2026"):
    print(chunk, end="", flush=True)

# 4. Deploy Cloud Run service
proj = zeni.projects.deploy(
    name="my-app",
    image="us-central1-docker.pkg.dev/zeni-cloud-core/zeni-images/my-app:v1",
    size="m",
)

# 5. SQL
rows = zeni.data.sql("SELECT * FROM kv LIMIT 10")

# 6. Web3 live read
stack = zeni.web3.zeni_stack()
print("$ZENI total supply:", stack["ZENI_TOKEN"]["total_supply"])

# 7. Cron
zeni.automation.create_cron(
    name="daily-report", schedule="0 9 * * *",
    target_url="https://my-app.run.app/api/cron",
    method="POST", timezone="Asia/Ho_Chi_Minh",
)

# 8. Billing
print(zeni.billing.subscription())
```

## Error handling

```python
from zenicloud import ZeniError

try:
    zeni.ai.generate_image(prompt="...")
except ZeniError as e:
    if e.status == 402: print("Hết quota")
    if e.status == 429: print("Rate limited")
```

## License

MIT · © Zeni Holdings 2026
