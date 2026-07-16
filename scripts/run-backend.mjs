// 跨平台启动后端：自动探测项目根目录 venv 中的 Python 解释器
// 支持 Windows (venv/Scripts/python.exe) 与 Linux/macOS (venv/bin/python)
import { spawn } from 'node:child_process';
import path from 'node:path';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(scriptDir, '..'); // 项目根目录 (ai-football/)
const backendDir = path.join(root, 'backend');

function findPython() {
  const candidates = [
    path.join(root, 'venv', 'Scripts', 'python.exe'), // Windows
    path.join(root, 'venv', 'bin', 'python'),          // Linux / macOS
    path.join(root, 'venv', 'bin', 'python3'),
  ];
  for (const c of candidates) {
    if (fs.existsSync(c)) return c;
  }
  // 回退：依赖当前已激活的虚拟环境或系统 python/python3
  return process.platform === 'win32' ? 'python' : 'python3';
}

const python = findPython();
console.log(`[backend] 使用 Python: ${python}`);
console.log(`[backend] 工作目录: ${backendDir}`);

const child = spawn(
  python,
  ['-m', 'uvicorn', 'main:app', '--host', '0.0.0.0', '--port', '8000', '--reload'],
  { cwd: backendDir, stdio: 'inherit', shell: false }
);

child.on('exit', (code) => process.exit(code ?? 1));
process.on('SIGINT', () => child.kill('SIGINT'));
process.on('SIGTERM', () => child.kill('SIGTERM'));
