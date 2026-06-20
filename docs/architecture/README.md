# Architecture Diagram

`architecture.png` — AWS 公式アイコンで描いた Generic Game Server 全体構成図。
[diagram.py](diagram.py) が図の唯一のソースで、これを変更・再生成して管理する。

## 初回セットアップ

```bash
# 1. graphviz（dot バイナリ）— レンダリングに必須
sudo apt-get update && sudo apt-get install -y graphviz

# 2. venv + diagrams（プロジェクトルートから実行）
python3 -m venv docs/architecture/.venv
docs/architecture/.venv/bin/pip install "diagrams==0.23.4"
```

## 再生成

```bash
# プロジェクトルートから実行
docs/architecture/.venv/bin/python docs/architecture/diagram.py
# → docs/architecture/architecture.png が更新される
```

## 保守ルール

Terraform リソースを追加・変更したときは `diagram.py` も合わせて更新し、
再生成した `architecture.png` を一緒にコミットする。

`.venv/` は `.gitignore` 済みのためコミット不要。
