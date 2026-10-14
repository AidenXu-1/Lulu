import {build} from 'esbuild';
import {readFile, writeFile, readdir} from 'node:fs/promises';
import {dirname, join, resolve} from 'node:path';
import {createHash} from 'node:crypto';

const result=await build({entryPoints:['workspace.js'],bundle:true,outfile:'assets/workspace.js',format:'iife',
  target:['chrome112'],minify:true,legalComments:'eof',metafile:true});
const packages=new Map();
for(const input of Object.keys(result.metafile.inputs)){
  if(!input.startsWith('node_modules/'))continue;
  let folder=dirname(resolve(input));
  while(folder!==dirname(folder)){
    try{
      const pkg=JSON.parse(await readFile(join(folder,'package.json'),'utf8'));
      if(pkg.name&&pkg.version){
        const id=`${pkg.name}@${pkg.version}`;
        if(!packages.has(id)){
          const files=(await readdir(folder)).filter(name=>/^(license|copying|notice)(?:[.-]|$)/i.test(name));
          const notices=await Promise.all(files.map(async name=>`${name}\n${await readFile(join(folder,name),'utf8')}`));
          if(!notices.length)throw Error(`License text missing for ${id}`);
          packages.set(id,{name:pkg.name,version:pkg.version,license:pkg.license,notices});
        }
        break;
      }
    }catch(error){if(error.code!=='ENOENT')throw error;}
    folder=dirname(folder);
  }
}
const sorted=[...packages.values()].sort((a,b)=>a.name.localeCompare(b.name));
await writeFile('assets/THIRD_PARTY_NOTICES.txt','Lulu document workspace bundled dependencies\n\n'+sorted.map(p=>`${p.name}@${p.version} (${p.license})\n${p.notices.join('\n')}`).join('\n\n'));
const assets={};
for(const name of (await readdir('assets')).sort()){
  if(name==='build-manifest.json')continue;
  const bytes=await readFile(join('assets',name));
  assets[name]={bytes:bytes.length,sha256:createHash('sha256').update(bytes).digest('hex')};
}
await writeFile('assets/build-manifest.json',JSON.stringify({dependencies:JSON.parse(await readFile('package.json','utf8')).dependencies,
  bundledPackages:sorted.map(({notices,...p})=>p),assets,
  inputs:Object.keys(result.metafile.inputs).sort(),outputs:result.metafile.outputs},null,2)+'\n');
