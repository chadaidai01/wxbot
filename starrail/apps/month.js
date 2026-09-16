const plugin = require( '../../lib/plugins/plugin.js')
const MysSRApi = require( '../runtime/MysSRApi.js')
const User = require( '../../genshin/model/user.js')
const setting = require( '../utils/setting.js')
const _ = require( 'lodash')
const fetch = require( 'node-fetch')
const {getCk, rulePrefix} = require( '../utils/common.js')
const gsCfg = require("../../genshin/model/gsCfg");
class Month extends plugin {
  constructor (ctx,session) {
    super({
      name: '星铁plugin-收入',
      dsc: '星穹铁道收入信息',
      /** https://oicqjs.github.io/oicq/#events */
      event: 'message',
      priority: setting.getConfig('gachaHelp').noteFlag ? 5 : 500,
      rule: [
        // {
        //   reg: `^${rulePrefix}(星琼获取|月历|月收入|收入|原石)$`,
        //   fnc: 'month'
        // }
      ],
      ctx:ctx,
      session:session
    })
    this.User = new User(this.e)
  }

  async month (e) {
    e = this.e
    let user = this.e.user_id
    let ck = await gsCfg.getBingCkSingle(this.e.user_id)
    if (!ck || Object.keys(ck).length==0) {
      await e.reply('尚未绑定cookie')
      return false
    }
    let targetCk = Object.keys(ck).filter(k => ck[k].isMain)
    if (!targetCk || targetCk.length == 0) {
      await e.reply('尚未绑定cookie')
      return false
    }
    ck[targetCk[0]].uid = ck[targetCk[0]].starrail_uid
    let uid = targetCk[0]
    let api = new MysSRApi(targetCk[0], ck)
    let deviceFp = await redis.get(`STARRAIL:DEVICE_FP:${uid}`)
    if (!deviceFp) {
      let sdk = api.getUrl('getFp')
      let res = await fetch(sdk.url, { headers: sdk.headers, method: 'POST', body: sdk.body })
      let fpRes = await res.json()
      deviceFp = fpRes?.data?.device_fp
      if (deviceFp) {
        await redis.set(`STARRAIL:DEVICE_FP:${uid}`, deviceFp, { EX: 86400 * 7 })
      }
    }
    const cardData = await api.getData('srMonth', { deviceFp })
    if (!cardData || cardData.retcode != 0) return e.reply(cardData.message || '请求数据失败')
    let data = cardData.data
    data.pieData = JSON.stringify(data.month_data.group_by.map((v) => {
      return {
        name: `${v.action_name} ${v.num}`,
        value: v.num
      }
    }))
    await e.runtime.render('StarRail-plugin', '/month/month.html', data)
  }

  async miYoSummerGetUid () {
    let key = `STAR_RAILWAY:UID:${this.e.user_id}`
    let ck = await getCk(this.e)
    if (!ck) return false
    // if (await redis.get(key)) return false
    // todo check ck
    let api = new MysSRApi('', ck)
    let userData = await api.getData('srUser')
    if (!userData?.data || _.isEmpty(userData.data.list)) return false
    userData = userData.data.list[0]
    let { game_uid: gameUid } = userData
    await redis.set(key, gameUid)
    await redis.setEx(
        `STAR_RAILWAY:userData:${gameUid}`,
        60 * 60,
        JSON.stringify(userData)
    )
    return userData
  }
}

module.exports = Month
