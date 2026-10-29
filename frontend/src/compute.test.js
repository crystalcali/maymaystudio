import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {connectionCommand,shellQuote} from './compute.js';
test('quote preserves literal shell metacharacters',()=>{
 const value="a'b $(echo should-not-run) `echo test`";
 assert.equal(execFileSync('bash',['-c','printf %s '+shellQuote(value)],{encoding:'utf8'}),value);
});
test('container command preserves environment values and uses one GPU',()=>{
 const pair={agent_image:'ghcr.io/owner/agent:commit',studio_url:'https://studio.example',pairing_code:"test'code"};
 const args=JSON.parse(execFileSync('bash',['-c','sudo(){ if [ "$1" = mkdir ]; then return 0; fi; "$@"; }; mountpoint(){ return 0; }; df(){ printf "Filesystem 1024-blocks Used Available Capacity Mounted\\n/dev/test 300000000 1 200000000 1 /ephemeral\\n"; }; docker(){ if [ "$1" = inspect ]; then return 1; fi; python3 -c \'import json,sys; print(json.dumps(sys.argv[1:]))\' "$@"; }; '+connectionCommand(pair)],{encoding:'utf8'}));
 assert.ok(args.includes('type=bind,src=/ephemeral/maymay-workspace,dst=/workspace'));assert.ok(args.includes('device=0'));assert.ok(args.includes('PAIRING_CODE='+pair.pairing_code));assert.equal(args.at(-1),pair.agent_image);
});
test('no guessed container image',()=>assert.equal(connectionCommand({}),''));
test('missing ephemeral mount aborts before Docker launch',()=>{
 const script='sudo(){ "$@"; }; docker(){ [ "$1" = inspect ] && return 1; echo unexpected-launch; }; mountpoint(){ return 1; }; '+connectionCommand({agent_image:'image',studio_url:'url',pairing_code:'code'});
 assert.throws(()=>execFileSync('bash',['-c',script],{stdio:'pipe'}),e=>e.status===1&&!e.stdout.toString().includes('unexpected-launch'));
});
test('existing agent is never replaced automatically',()=>{
 const script='sudo(){ "$@"; }; docker(){ [ "$1" = inspect ] && return 0; echo unexpected-launch; }; '+connectionCommand({agent_image:'image'});
 assert.throws(()=>execFileSync('bash',['-c',script],{stdio:'pipe'}),e=>e.status===1&&!e.stdout.toString().includes('unexpected-launch'));
});
test('unsafe bind syntax is rejected',()=>assert.ok(connectionCommand({agent_image:'image'},'/disk,a').startsWith('#')));
test('low disk space aborts before starting paid-idle worker',()=>{
 const script='sudo(){ if [ "$1" = mkdir ]; then return 0; fi; "$@"; }; docker(){ [ "$1" = inspect ] && return 1; echo unexpected-launch; }; mountpoint(){ return 0; }; df(){ printf "Filesystem blocks used available capacity mount\\n/dev/root 100 90 10 90 /\\n"; }; '+connectionCommand({agent_image:'image'});
 assert.throws(()=>execFileSync('bash',['-c',script],{stdio:'pipe'}),e=>e.status===1&&!e.stdout.toString().includes('unexpected-launch'));
});
