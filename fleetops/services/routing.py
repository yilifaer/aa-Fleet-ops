from collections import deque

from fleetops.services.sde import solar_system_names, stargate_edges


def systems_within_jumps(origin_system_id: int, max_jumps: int = 3) -> dict[int, int]:
    graph = stargate_edges()
    if not graph or not origin_system_id:
        return {int(origin_system_id): 0} if origin_system_id else {}
    distance = {int(origin_system_id): 0}
    queue = deque([int(origin_system_id)])
    while queue:
        node = queue.popleft()
        if distance[node] >= max_jumps:
            continue
        for neighbour in graph.get(node, ()):
            if neighbour not in distance:
                distance[neighbour] = distance[node] + 1
                queue.append(neighbour)
    return distance


def proximity_rows(operation, max_jumps: int = 3):
    fc = operation.member_states.filter(character_id=operation.fc_character_id, is_active=True).first()
    if not fc or not fc.solar_system_id:
        return []
    distances = systems_within_jumps(fc.solar_system_id, max_jumps)
    members = operation.member_states.filter(is_active=True, solar_system_id__in=distances.keys())
    grouped = {}
    for member in members:
        row = grouped.setdefault(member.solar_system_id, {"system_id": member.solar_system_id, "system_name": member.solar_system_name, "jump_distance": distances[member.solar_system_id], "members": []})
        row["members"].append(member)
    names = solar_system_names(grouped.keys())
    for sid, row in grouped.items():
        row["system_name"] = row["system_name"] or names.get(sid, str(sid))
    return sorted(grouped.values(), key=lambda r: (r["jump_distance"], r["system_name"]))
