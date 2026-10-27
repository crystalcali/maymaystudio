import { defineConfig } from 'vite';
export default defineConfig({server:{proxy:{'/api':'http://localhost:8000','/objects':'http://localhost:8000'}}});
