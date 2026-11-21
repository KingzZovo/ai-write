"""Conditional author profile writes under the Neo4j node write lock."""
import json
from uuid import uuid4


class CharacterProfileConflict(ValueError):
    pass


def same_profile(left, right):
    return json.dumps(left, ensure_ascii=False, sort_keys=True) == json.dumps(right, ensure_ascii=False, sort_keys=True)


async def write_character_profile(driver, project_id, name, profile, expected):
    async def write(tx):
        node_id = str(uuid4())
        sync_token = str(uuid4())
        # The dependent increment acquires the node write lock before reading
        # the profile. A failed comparison rolls it back with the transaction.
        prefix = ("MERGE (c:Character {project_id:$pid,name:$name}) ON CREATE SET c.id=$id "
                  if expected is None else "MATCH (c:Character {project_id:$pid,name:$name}) ")
        result = await tx.run(prefix +
            "SET c.profile_write_lock=coalesce(c.profile_write_lock,0)+1 "
            "RETURN c.id AS id,c.profile_json AS profile_json",
            pid=str(project_id), name=name, id=node_id)
        row = await result.single()
        if row is None:
            raise CharacterProfileConflict('人物已被删除或尚未同步，草稿已保留，请刷新核对。')
        created = expected is None and row['id'] == node_id
        try:
            current = json.loads(row['profile_json']) if row['profile_json'] is not None else {}
            if not isinstance(current, dict): raise ValueError('invalid profile')
        except (ValueError, TypeError):
            raise CharacterProfileConflict('当前人物档案格式异常，未覆盖原档案，请先核对。') from None
        # The desired value already present is an idempotent retry, including
        # a graph success whose later PG projection/HTTP response was lost.
        if not created and not same_profile(current, profile) and (expected is None or not same_profile(current, expected)):
            raise CharacterProfileConflict('人物档案已被修改，未覆盖新版本。草稿已保留，请刷新核对后重试。')
        result = await tx.run(
            "MATCH (c:Character {project_id:$pid,name:$name}) "
            "SET c.profile_json=$profile,c.profile_origin='author_edit', "
            "c.profile_sync_token=$token,c.profile_sync_after=0,c.profile_sync_attempts=0",
            pid=str(project_id), name=name, profile=json.dumps(profile, ensure_ascii=False), token=sync_token)
        await result.consume()
        return sync_token
    async with driver.session() as session:
        return await session.execute_write(write)
