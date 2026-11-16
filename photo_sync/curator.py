from photo_sync import flow


def handler(event, context):
    return {"proposal_id": flow.start_proposal(event["collection_id"])}
