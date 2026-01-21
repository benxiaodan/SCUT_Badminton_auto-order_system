# 华工羽毛球订场系统 (SCUT Badminton Auto-Order System)

## 项目简介
这是一个基于 Flask 和 React 的华工羽毛球场自动订场系统。

## 部署教程

### 1. 环境准备
在部署服务器上，请确保已安装以下环境：
- **Node.js**: 推荐使用最新的 LTS 版本。
- **Python**: 3.8 或以上版本。
- **Google Chrome**: 用于 Selenium 自动化操作。
- **ChromeDriver**: 与安装的 Chrome 版本匹配的驱动程序。

### 2. 安装依赖
首先，克隆项目代码到本地或服务器。

#### 前端依赖
**重要**：请务必先通过命令行安装 npm 及其依赖，这是构建项目的前提。
```bash
npm install
```

#### 后端依赖
```bash
pip install -r requirements.txt
```

### 3. 构建前端 (Build)
在项目根目录下运行以下命令，将前端代码编译为静态文件：
```bash
npm run build
```
构建完成后，会在根目录下生成一个 `dist` 文件夹，其中包含了部署所需的静态资源。

### 4. 服务器部署
1. **配置文件**：
   - 复制 `.env.example` 为 `.env`，并填入必要的配置信息（如 API Key 等）。
   - 创建或修改 `allowed_users.txt`，按需添加允许访问的用户名单。
   
2. **启动服务**：
   确保 `dist` 文件夹存在（即已成功执行 `npm run build`），然后运行 Python 服务器：
   ```bash
   python server.py
   ```

3. **访问系统**：
   服务器启动后，默认运行在 `http://localhost:5000` (或服务器对应的 IP 地址)。

## 注意事项
- 本项目在 GitHub 仓库中仅包含用于 `npm run build` 的关键源文件（不包含 `node_modules`、`dist` 等构建产物）。
- 部署时请确保服务器上有完整的 Python 运行环境和 Chrome 浏览器环境。
