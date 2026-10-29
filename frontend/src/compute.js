export const shellQuote=value=>"'"+String(value).replaceAll("'","'\\''")+"'";
export function connectionCommand(pair,workspace='/ephemeral/maymay-workspace'){
 if(!pair.agent_image)return '';
 if(!workspace.startsWith('/')||workspace.includes(',')||/[\r\n]/.test(workspace))return '# Укажите абсолютный путь без запятых и переводов строк';
 const launch=['sudo docker run -d --init --gpus '+shellQuote('device=0')+' --restart unless-stopped',
 '--name maymay-agent --mount '+shellQuote('type=bind,src='+workspace+',dst=/workspace'),
 '-e '+shellQuote('STUDIO_URL='+pair.studio_url),
 '-e '+shellQuote('PAIRING_CODE='+pair.pairing_code),
 '-e '+shellQuote('WORKER_NAME=MayMay-GPU'),shellQuote(pair.agent_image)].join(' \\\n  ');
 return ['(', 'set -eu',
 'if sudo docker inspect maymay-agent >/dev/null 2>&1; then echo "Агент уже существует. Используйте инструкцию переноса, данные не изменены." >&2; exit 1; fi',
 ...(workspace.startsWith('/ephemeral/')?['mountpoint -q /ephemeral || { echo "Диск /ephemeral не смонтирован. Выберите рабочий диск." >&2; exit 1; }']:[]),
 'sudo mkdir -p -- '+shellQuote(workspace),
 'available=$(df -Pk -- '+shellQuote(workspace)+" | awk 'NR==2 {print $4}')",
 'if [ "$available" -lt 157286400 ]; then echo "Недостаточно места: для нового агента нужно 150 GiB свободного места. Выберите большой диск." >&2; exit 1; fi',
 launch,')'].join('\n');
}
