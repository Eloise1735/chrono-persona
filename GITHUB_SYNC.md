# GitHub 同步说明（本仓库）

固定信息：

- **GitHub 用户**：`Eloise1735`
- **仓库名**：`chrono-persona`
- **HTTPS 克隆地址**：`https://github.com/Eloise1735/chrono-persona.git`
- **SSH 克隆地址**：`git@github.com:Eloise1735/chrono-persona.git`

---

## 本机（Windows）：上传更新

在工程目录下执行（路径按你本机实际为准）：

```powershell
cd "d:\Eloise\coding\凯尔希状态机"
git status
git add -A
git commit -m "简要说明本次改动"
git push
```

首次若未设置上游分支：

```powershell
git push -u origin main
```

---

## 云服务器（Linux）：下载更新

### 1. 首次部署（仅此一次）

```bash
cd /你想放代码的目录
git clone https://github.com/Eloise1735/chrono-persona.git
cd chrono-persona
```

若使用 SSH 且已配置密钥：

```bash
git clone git@github.com:Eloise1735/chrono-persona.git
cd chrono-persona
```

然后在本机**单独**准备运行环境（示例）：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
# 编辑 config.yaml，填入 API 与路径等
mkdir -p data
```

### 2. 日常更新代码

```bash
cd /你的路径/chrono-persona
git fetch origin
git pull origin main
```

若你改过服务器上的跟踪文件且与远程冲突，需要先处理冲突或暂存本地改动后再 `pull`（一般**不要**在服务器上直接改业务代码，改动在本机提交再拉取）。

### 3. 拉代码后重启服务

根据你如何启动进程选择其一，例如：

```bash
# systemd 示例（服务名按你实际为准）
sudo systemctl restart chrono-persona

# 或手动：先停掉旧 uvicorn，再在项目目录激活 venv 后重新启动
```

仅修改静态前端（`web/`）时，若进程未缓存文件，有时可不重启；**修改了 `server/` 下 Python 代码则必须重启**。

---

## 数据库 `data/kelsey.db` 会上传到 GitHub 吗？

**不会。**

仓库根目录的 `.gitignore` 已包含：

- `data/`（整个数据目录）
- `*.db`（任意 SQLite 等数据库文件）

因此 **`data/kelsey.db` 不会被 `git add` 进版本库，也就不会出现在 GitHub 上**。  
每台机器（本机、云服务器）各自保留自己的 `data/` 与 `config.yaml`，互不影响；备份数据库请自行复制文件或使用你的备份脚本，不要依赖 Git。

---

## 小结

| 内容           | 是否进 Git / GitHub      |
|----------------|---------------------------|
| 代码、`web/` 等 | 是                        |
| `config.yaml`  | 否（含密钥，勿提交）       |
| `data/kelsey.db` | 否                      |
| `config.example.yaml` | 是（示例，无真实密钥） |
