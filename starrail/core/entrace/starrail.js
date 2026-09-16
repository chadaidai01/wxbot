class StarRailApp {
  constructor(app, ctx, config) {
    ctx.guild().command('genshin.startrail.bindSRUid', {authority: 1})
      .alias('*绑定uid')
      .shortcut(/^\*绑定(uid|UID| )?[1-9][0-9]{8}$/)
      .action(async ({session, options}, input) => {
        let uid = session.content.replace(/\s*/g, "").match(/[1|2|5-9][0-9]{8}/g)
        if (uid) {
          session.content = uid.toString()
          new app.hkrpg(ctx, session).bindSRUid()
          return
        }
        await session.send('请发送 uid')
        uid = (await session.prompt())
        if (!uid) {
          session.send('绑定失败，UID为空，请重新发送#绑定uid，如果是QQ频道需要艾特机器人')
          return
        }
        session.content = uid.toString()
        new app.hkrpg(ctx, session).bindSRUid()
      })

    ctx.guild().command('genshin.startrail.card', {authority: 1})
      .alias('*探索')
      .shortcut(/^\*(星铁|星轨|崩铁|星穹铁道)(卡片|探索)$/)
      .action(async ({session, options}, input) => {
        new app.hkrpg(ctx, session).card()
      })
    ctx.guild().command('genshin.startrail.note', {authority: 1})
      .alias('*体力')
      .shortcut(/^\*(星铁|星轨|崩铁|星穹铁道)?体力$/)
      .action(async ({session, options}, input) => {
        new app.note(ctx, session).note()
      })
    ctx.guild().command('genshin.startrail.month', {authority: 1})
      .alias('*收入')
      .alias('*原石')
      .shortcut(/^#(星铁|星轨|崩铁|星穹铁道)(星琼获取|月历|月收入|收入|原石)$/)
      .action(async ({session, options}, input) => {
        new app.month(ctx, session).month()
      })
    // ctx.guild().command('genshin.startrail.avatar', {authority: 1})
    //   .shortcut(/^\*(星铁|星轨|崩铁|星穹铁道)?(.*)面板(更新)?/)
    //   .action(async ({session, options}, input) => {
    //     new app.panel(ctx, session).panel()
    //   })
    ctx.guild().command('genshin.startrail.help', {authority: 1})
      .shortcut(/^\*(星铁|星轨|崩铁|星穹铁道)?帮助$/)
      .action(async ({session, options}, input) => {
        new app.hkrpg(ctx, session).help()
      })
  }
}

module.exports = StarRailApp
