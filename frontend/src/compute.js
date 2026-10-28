export const shellQuote=value=>"'"+String(value).replaceAll("'","'\\''")+"'";
export function connectionCommand(pair){
 if(!pair.agent_image)return '';
 return ['docker run -d --init --gpus '+shellQuote('device=0')+' --restart unless-stopped',
 '--name maymay-agent -v maymay-workspace:/workspace',
 '-e '+shellQuote('STUDIO_URL='+pair.studio_url),
 '-e '+shellQuote('PAIRING_CODE='+pair.pairing_code),
 '-e '+shellQuote('WORKER_NAME=MayMay-GPU'),shellQuote(pair.agent_image)].join(' \\\n  ');
}
