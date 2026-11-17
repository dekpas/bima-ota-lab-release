#!/usr/bin/env python3
# BIMA OTA v0.1.2 -- UJI VISUAL khusus RTU-MF-068.
# GET /flows dengan API v2, batasi 1 node ui_template, deploy mode "nodes".
# TIDAK untuk fleet/produksi otomatis. Rencanakan maintenance sebelum APPLY.
import argparse
import copy
import datetime
import getpass
import hashlib
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = 'http://127.0.0.1:1880'
MANIFEST_URL = 'https://raw.githubusercontent.com/dekpas/bima-ota-lab-release/main/manifest-v0.1.2.json'
MANIFEST_SHA256 = '925cf66e4f532a11dd7bbf3999ad0da8c11471907c5d253b3150285e3baba1b2'
TARGET_ID = 'fc7c17b4305433d6'
TARGET_NAME = 'UI_System Settings'
TARGET_GROUP = '53d4e5349395de93'
OLD_SHA256 = '867478d21fc996b5a9b03019f7799372b821b1464ccd50b421c43d542d496167'
NEW_SHA256 = '6abf9359c56aadd8d5e89ec649a4cb6f887941be171de1bb7aac754845bd2dbe'
BACKUP_DIR = pathlib.Path.home() / '.bima-ota-visual-backups'


def sha256(txt):
    return hashlib.sha256(txt.encode('utf-8')).hexdigest()


def require(condition, reason):
    if not condition:
        raise ValueError('Manifest tidak valid: ' + reason)


def fetch_manifest():
    request = urllib.request.Request(MANIFEST_URL, headers={'User-Agent':'BIMA-OTA-Visual-LAB/0.1.2'})
    with urllib.request.urlopen(request, timeout=25) as r:
        raw = r.read(30000)
    if hashlib.sha256(raw).hexdigest() != MANIFEST_SHA256:
        raise ValueError('Manifest HTTPS berbeda dari SHA256 release yang disetujui; ABORT')
    data = json.loads(raw)
    require(data.get('schema') == 'bima-ota-ui-patch-v1', 'schema')
    require(data.get('version') == '0.1.2', 'version')
    require(data.get('target') == {'nodeId':TARGET_ID,'type':'ui_template','name':TARGET_NAME,'group':TARGET_GROUP,'property':'format'}, 'target')
    require(data.get('baseFormatSha256') == OLD_SHA256, 'base format hash')
    require(data.get('updatedFormatSha256') == NEW_SHA256, 'updated format hash')
    require(data.get('labOnly') is True, 'labOnly')
    find = data.get('patch',{}).get('find')
    replace = data.get('patch',{}).get('replace')
    require(isinstance(find,str) and isinstance(replace,str) and find != replace, 'patch')
    return data


def get_token():
    username = input('Username Node-RED utama (Admin API): ').strip()
    password = getpass.getpass('Password Node-RED: ')
    body = urllib.parse.urlencode({'client_id':'node-red-admin','grant_type':'password','scope':'*',
                                   'username':username,'password':password}).encode('utf-8')
    req=urllib.request.Request(BASE_URL+'/auth/token',data=body,method='POST',
        headers={'Content-Type':'application/x-www-form-urlencoded'})
    with urllib.request.urlopen(req,timeout=20) as r:
        token=json.load(r)['access_token']
    del password
    return token


def api(token, method='GET', obj=None, deployment=None):
    headers={'Authorization':'Bearer '+token,'Node-RED-API-Version':'v2'}
    data=None
    if obj is not None:
        headers['Content-Type']='application/json'
        data=json.dumps(obj,ensure_ascii=False,separators=(',',':')).encode('utf-8')
    if deployment:
        headers['Node-RED-Deployment-Type']=deployment
    req=urllib.request.Request(BASE_URL+'/flows',headers=headers,data=data,method=method)
    with urllib.request.urlopen(req,timeout=35) as r:
        return json.load(r)


def target_node(response):
    if not isinstance(response,dict) or not isinstance(response.get('rev'),str) or not isinstance(response.get('flows'),list):
        raise ValueError('Expected Node-RED Admin API v2 with revision')
    flows=response['flows']
    # Proteksi lingkungan: harus flow utama, bukan baseline LAB.
    if any(n.get('id')=='bima_ota_lab_marker_v1' for n in flows):
        raise ValueError('Node-RED LAB terdeteksi; target ini khusus service utama')
    selected=[n for n in flows if n.get('id')==TARGET_ID]
    if len(selected)!=1:
        raise ValueError('Node ID target tidak tepat satu kali')
    node=selected[0]
    if (node.get('type'),node.get('name'),node.get('group'))!=('ui_template',TARGET_NAME,TARGET_GROUP):
        raise ValueError('Tipe/nama/group UI System Settings berbeda, ABORT')
    if not isinstance(node.get('format'),str):
        raise ValueError('Property format tidak valid')
    return node


def build_patched(current,manifest):
    source=target_node(current)['format']
    digest=sha256(source)
    if digest==NEW_SHA256:
        return None,digest
    if digest!=OLD_SHA256:
        raise ValueError('Hash kode saat ini tidak sama dengan baseline; tidak boleh overwrite')
    find=manifest['patch']['find']; replace=manifest['patch']['replace']
    if source.count(find)!=1:
        raise ValueError('Anchor patch tidak unik, ABORT')
    out=source.replace(find,replace,1)
    if sha256(out)!=NEW_SHA256:
        raise ValueError('Hash hasil patch tidak sesuai manifest')
    return out,digest


def save_backup(response,original_fmt):
    oldmask=os.umask(0o077)
    try:
        BACKUP_DIR.mkdir(parents=True,exist_ok=True,mode=0o700)
        os.chmod(BACKUP_DIR,0o700)
        filename='ui-system-settings-before-v012-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.json'
        target=BACKUP_DIR/filename
        if target.exists():
            raise RuntimeError('File backup sudah ada, ulangi beberapa detik kemudian')
        # snapshot full flows private: mungkin mengandung konfigurasi sensitif
        body={'targetId':TARGET_ID,'oldFormatSha256':OLD_SHA256,'candidateSha256':NEW_SHA256,
              'capturedAtUtc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'oldFormat':original_fmt,'snapshot':response}
        with target.open('x',encoding='utf-8') as f:
            json.dump(body,f,ensure_ascii=False,indent=2)
        os.chmod(target,0o600)
        return target
    finally:
        os.umask(oldmask)


def deploy(token,ref,fmt):
    changed=copy.deepcopy(ref)
    found=target_node(changed)
    found['format']=fmt
    # Tetap sertakan rev: POST akan menolak bila ada perubahan lain (HTTP 409).
    # nodes: hanya node yang dimodifikasi yang di-redeploy, bukan full.
    return api(token,method='POST',obj=changed,deployment='nodes')


def check(token,manifest):
    current=api(token)
    candidate,digest=build_patched(current,manifest)
    print('Node-RED revision:',current['rev'])
    print('Total node:',len(current['flows']))
    print('Node target:',TARGET_ID)
    print('Current SHA256:',digest)
    print('Candidate SHA256:',NEW_SHA256)
    if candidate is None:
        print('ALREADY APPLIED. Tidak ada perubahan.')
    else:
        print('CHECK OK — hanya 1 perubahan HTML pada title-bar. Tidak ada deploy.')


def apply(token,manifest):
    # Operator harus memastikan safety / maintenance secara terpisah.
    print('PERINGATAN: service utama mengendalikan hardware. Deploy dapat memicu lifecycle node.')
    print('Pastikan maintenance, backup full .node-red, dan panel dalam kondisi aman.')
    answer=input('Ketik APPLY '+TARGET_ID+' untuk konfirmasi: ').strip()
    if answer!='APPLY '+TARGET_ID:
        raise RuntimeError('Dibatalkan; tidak ada perubahan.')
    # Always re-read right before applying to enforce revision + baseline.
    current=api(token)
    candidate,digest=build_patched(current,manifest)
    if candidate is None:
        print('ALREADY APPLIED; tidak deploy ulang.')
        return
    backup=save_backup(current,target_node(current)['format'])
    print('Backup PRIVATE:',backup)
    result=deploy(token,current,candidate)
    print('Deploy response rev:',result.get('rev'))
    after=api(token)
    newdigest=sha256(target_node(after)['format'])
    print('Current SHA256:',newdigest)
    if newdigest!=NEW_SHA256:
        raise RuntimeError('Hash verifikasi pasca deploy berbeda. STOP, gunakan backup untuk investigasi.')
    print('SUCCESS: OTA VISUAL v0.1.2 APPLIED')
    print('ROLLBACK: python3 run_bima_ota_visual_v012.py --rollback '+str(backup))


def rollback(token,path):
    bp=pathlib.Path(path).expanduser().resolve()
    if not bp.is_file() or not bp.is_relative_to(BACKUP_DIR.expanduser().resolve()):
        raise ValueError('Backup harus berada di folder private ~/.bima-ota-visual-backups')
    data=json.loads(bp.read_text(encoding='utf-8'))
    if data.get('targetId')!=TARGET_ID or data.get('oldFormatSha256')!=OLD_SHA256 or data.get('candidateSha256')!=NEW_SHA256:
        raise ValueError('Metadata backup tidak sesuai')
    old=data.get('oldFormat')
    if not isinstance(old,str) or sha256(old)!=OLD_SHA256:
        raise ValueError('Isi backup rusak atau tidak cocok')
    current=api(token)
    existing=target_node(current)['format']
    if sha256(existing)!=NEW_SHA256:
        raise ValueError('Kode saat ini bukan v0.1.2; jangan rollback untuk menghindari menimpa perubahan lain')
    # Rollback only format, using latest rev and rest of current flows untouched.
    ans=input('Ketik ROLLBACK '+TARGET_ID+' untuk mengembalikan UI: ').strip()
    if ans!='ROLLBACK '+TARGET_ID:
        raise RuntimeError('Dibatalkan; tidak ada perubahan.')
    # Re-read and compare again to avoid overwriting intervening changes.
    current=api(token)
    if sha256(target_node(current)['format'])!=NEW_SHA256:
        raise ValueError('UI berubah sesudah konfirmasi; batal')
    result=deploy(token,current,old)
    after=api(token)
    digest=sha256(target_node(after)['format'])
    if digest!=OLD_SHA256:
        raise RuntimeError('Rollback tidak terverifikasi! Periksa flow dan backup manual.')
    print('SUCCESS: ROLLBACK KE BASELINE UI');print('Restored SHA256:',digest)


def main():
    p=argparse.ArgumentParser(description='OTA v0.1.2 visual demo, targeted and guarded for Node-RED main 1880')
    mg=p.add_mutually_exclusive_group(required=True)
    mg.add_argument('--check',action='store_true')
    mg.add_argument('--apply',action='store_true')
    mg.add_argument('--rollback',metavar='BACKUP_PATH')
    args=p.parse_args()
    try:
        manifest=fetch_manifest() if not args.rollback else None
        token=get_token()
        if args.check: check(token,manifest)
        elif args.apply: apply(token,manifest)
        else: rollback(token,args.rollback)
    except urllib.error.HTTPError as e:
        print('HTTP ERROR:',e.code,'(cek hak Admin API, revisi flow, atau repository)',file=sys.stderr)
        return 1
    except Exception as e:
        print('ABORT:',str(e),file=sys.stderr)
        return 1
    return 0

if __name__=='__main__':
    sys.exit(main())
