const fs = require( 'node:fs')
const common = require('../lib/common/common')

const files = fs.readdirSync(`${common.getPluginsPath()}/StarRail-plugin/apps`).filter(file => file.endsWith('.js'))

async function init() {
  let ret = []
  files.forEach((file) => {
    ret.push(import(`./apps/${file}`))
  })
  ret = await Promise.allSettled(ret)
  let apps = {}
  for (let i in files) {
    let name = files[i].replace('.js', '')
    if (ret[i].status != 'fulfilled') {
      console.log(`载入插件错误：${name}`)
      console.log(ret[i].reason)
      continue
    }
    apps[name] = ret[i].value[Object.keys(ret[i].value)[0]]
  }
  return apps
}

module.exports = init
