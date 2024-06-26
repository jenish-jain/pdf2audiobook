# Lint as: python3
# Copyright 2020 Google LLC. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Usage:
# gcloud functions deploy p2a_gcs_trigger --runtime python37 --trigger-bucket <bucket> --memory=2048MB --timeout=540
#

import base64
import os
import re
import csv
import io
import tempfile
import ghostscript
import locale
import glob
import time
import pandas as pd
import jenkspy

from pydub import AudioSegment

from google.cloud import storage
from google.cloud import vision
from google.cloud import texttospeech
from google.cloud import automl_v1beta1 as automl
from google.protobuf import json_format

# break length
SECTION_BREAK = 2  # sec
CAPTION_BREAK = 1.5  # sec

# prediction labels
LABEL_BODY = "body"
LABEL_HEADER = "header"
LABEL_CAPTION = "caption"
LABEL_OTHER = "other"
FEATURE_CSV_HEADER = "id,text,chars,width,height,area,char_size,pos_x,pos_y,aspect,word_count,average_word_height,layout"

# ML API clients
project_id = os.environ["GCP_PROJECT"]
vision_client = vision.ImageAnnotatorClient()
storage_client = storage.Client()
speech_client = texttospeech.TextToSpeechClient()


def p2a_gcs_trigger(file, context):

    print("Trigger received for pdf to audio conversion {}".format(file))
    # get bucket and blob
    file_name = file["name"]
    bucket = None
    file_blob = None
    while bucket == None or file_blob == None:  # retry
        bucket = storage_client.get_bucket(file["bucket"])
        file_blob = bucket.get_blob(file_name)
        time.sleep(1)

    # OCR
    if file_name.lower().endswith(".pdf"):
        p2a_ocr_pdf(bucket, file_blob)
        return

    # predict
    if file_name.lower().endswith(".json"):
        p2a_predict(bucket, file_blob)
        return

    # generate speech (or generate labels for annotation)
    if file_name.lower().endswith(".csv"):
        p2a_generate_speech(bucket, file_blob)
        return


def p2a_ocr_pdf(bucket, pdf_blob):

    print("starting with OCR for file {}".format(pdf_blob.name))
    # define input config
    gcs_source_uri = "gs://{}/{}".format(bucket.name, pdf_blob.name)
    gcs_source = vision.types.GcsSource(uri=gcs_source_uri)
    feature = vision.types.Feature(
        type=vision.enums.Feature.Type.DOCUMENT_TEXT_DETECTION
    )
    input_config = vision.types.InputConfig(
        gcs_source=gcs_source, mime_type="application/pdf"
    )

    # define output config
    pdf_id = pdf_blob.name.replace(".pdf", "")[:4]  # use the first <4 chars as pdf_id
    gcs_dest_uri = "gs://{}/{}".format(bucket.name, pdf_id + ".")
    gcs_destination = vision.types.GcsDestination(uri=gcs_dest_uri)
    output_config = vision.types.OutputConfig(
        gcs_destination=gcs_destination, batch_size=100
    )

    # call the API
    async_request = vision.types.AsyncAnnotateFileRequest(
        features=[feature], input_config=input_config, output_config=output_config
    )
    async_response = vision_client.async_batch_annotate_files(requests=[async_request])
    print("Started OCR for file {}".format(pdf_blob.name))
    print("done with OCR response {}".format(async_response))

def p2a_predict(bucket, json_blob):

    print("starting with prediction for file {}".format(json_blob.name))
    # get pdf id and first page number
    m = re.match("(.*).output-([0-9]+)-.*", json_blob.name)
    pdf_id = m.group(1)
    first_page = int(m.group(2))

    # read the json file
    csv = build_feature_csv(json_blob, pdf_id, first_page)

    # save the feature CSV file for prediction
    feature_file_name = "{}-{:03}-features.csv".format(pdf_id, first_page)
    feature_blob = bucket.blob(feature_file_name)
    feature_blob.upload_from_string(csv)
    # print("Feature CSV file saved: {}".format(feature_file_name))
    json_blob.delete()


def build_feature_csv(json_blob, pdf_id, first_page):

    # parse json
    json_string = json_blob.download_as_string()
    json_response = json_format.Parse(json_string, vision.types.AnnotateFileResponse())

    # covert the json file to a bag of CSV lines.
    csv = FEATURE_CSV_HEADER + "\n"
    page_count = first_page
    for resp in json_response.responses:
        para_count = 0
        for page in resp.full_text_annotation.pages:

            # collect para features for the page
            page_features = []
            for block in page.blocks:
                if str(block.block_type) != "1":  # process only TEXT blocks
                    continue
                for para in block.paragraphs:
                    para_id = "{}-{:03}-{:03}".format(pdf_id, page_count, para_count)
                    f = extract_paragraph_feature(para_id, para)
                    page_features.append(f)
                    para_count += 1

            # output to csv
            for f in page_features:
                csv += '{},"{}",{},{:.6f},{:.6f},{:.6f},{:.6f},{:.6f},{:.6f},{:.6f},{:.6f},{:.6f},{}\n'.format(
                    f["para_id"],
                    f["text"],
                    f["chars"],
                    f["width"],
                    f["height"],
                    f["area"],
                    f["char_size"],
                    f["pos_x"],
                    f["pos_y"],
                    f["aspect"],
                    f["word_count"],
                    f["average_word_height"],
                    f["layout"],
                )

        page_count += 1

    print("csv {}".format(csv))
    df = pd.read_csv(io.StringIO(csv))
    breaks = jenkspy.jenks_breaks(df["average_word_height"], n_classes=6)
    print("breaks {}".format(breaks))

    df["labels"] = df.apply(
        lambda x: get_label(breaks, x["average_word_height"], x["layout"]), axis=1
    )
    df["classification_index"] = df.apply(
        lambda x: get_classification_index(breaks, x["average_word_height"]), axis=1
    )
    return df.to_csv()


def get_label(breaks, size, orientation):
    label = LABEL_OTHER
    if orientation == "v":
        label = LABEL_OTHER
    else:
        if size < breaks[2]:
            label = LABEL_OTHER
        elif size >= breaks[2] and size < breaks[3]:
            label = LABEL_CAPTION
        elif size >= breaks[3] and size < breaks[4]:
            label = LABEL_BODY
        elif size >= breaks[4]:
            label = LABEL_HEADER
    return label


def get_classification_index(breaks, size):
    for i in range(len(breaks)):
        if size < breaks[i]:
            return i


def extract_paragraph_feature(para_id, para):

    # collect text
    text = ""
    w_height_list = []
    for word in para.words:
        for symbol in word.symbols:
            text += symbol.text
            if hasattr(symbol.property, "detected_break"):
                break_type = symbol.property.detected_break.type
                if str(break_type) == "1":
                    text += " "  # if the break is SPACE
        w_y_list = []
        for vertices in word.bounding_box.normalized_vertices:
            w_y_list.append(vertices.y)
            word_height = max(w_y_list) - min(w_y_list)
            w_height_list.append(word_height)


    # remove double quotes
    text = text.replace('"', "")

    # remove URLs
    text = re.sub("https?://[\w/:%#\$&\?\(\)~\.=\+\-]+", "", text)

    # extract bounding box features
    x_list = []
    y_list = []
    for v in para.bounding_box.normalized_vertices:
        x_list.append(v.x)
        y_list.append(v.y)
    f = {}
    f["para_id"] = para_id
    f["text"] = text
    f["width"] = max(x_list) - min(x_list)
    f["height"] = max(y_list) - min(y_list)
    f["area"] = f["width"] * f["height"]
    f["chars"] = len(text)
    f["char_size"] = f["area"] / f["chars"] if f["chars"] > 0 else 0
    f["pos_x"] = (f["width"] / 2.0) + min(x_list)
    f["pos_y"] = (f["height"] / 2.0) + min(y_list)
    f["aspect"] = f["width"] / f["height"] if f["height"] > 0 else 0
    f["layout"] = "h" if f["aspect"] > 1 else "v"
    f["word_count"] = len(w_height_list)
    f["average_word_height"] = sum(w_height_list) / len(w_height_list)

    return f


def p2a_generate_speech(bucket, csv_blob):

    # parse prediction results from AutoML
    batch_id, sorted_ids, text_dict, label_dict = parse_prediction_results(
        bucket, csv_blob
    )

    # generate mp3 files with the parsed results
    mp3_blob_list = generate_mp3_files(bucket, sorted_ids, text_dict, label_dict)

    # merge mp3 files
    merge_mp3_files(bucket, batch_id, mp3_blob_list)

    # delete prediction result (tables_1.csv) files
    folder_name = re.sub("/.*.csv", "", csv_blob.name)
    folder_blobs = storage_client.list_blobs(bucket, prefix=folder_name)
    for b in folder_blobs:
        b.delete()


def parse_prediction_results(bucket, csv_blob):
    # parse CSV
    csv_string = csv_blob.download_as_string().decode("utf-8")
    csv_file = io.StringIO(csv_string)
    reader = csv.DictReader(csv_file)
    text_dict = {}
    label_dict = {}
    for row in reader:

        # build text_dict
        id = row["id"]
        text = row["text"]
        text = text.replace("<", "")  # remove all '<'s for escaping in SSML
        text_dict[id] = text
        label_dict[id] = row["labels"]

    sorted_ids = sorted(text_dict.keys())
    first_id = sorted_ids[0]

    # remove the OTHER paras
    others = ""
    for id in sorted_ids:
        if label_dict[id] == LABEL_OTHER:
            others += text_dict[id] + " "
            sorted_ids.remove(id)

    # merging subsequent paragraphs
    last_id = None
    period_pattern = re.compile(r"^.*[.。」）)”]$")
    for id in sorted_ids:
        if last_id:
            is_bodypairs = (
                label_dict[id] == LABEL_BODY and label_dict[last_id] == LABEL_BODY
            )
            is_captpairs = (
                label_dict[id] == LABEL_CAPTION and label_dict[last_id] == LABEL_CAPTION
            )
            is_lastbody_nopediod = not period_pattern.match(text_dict[last_id])
            if (is_bodypairs and is_lastbody_nopediod) or is_captpairs:
                text_dict[id] = text_dict[last_id] + text_dict[id]
                sorted_ids.remove(last_id)
        last_id = id

    # get batch_id (pdf id + the first page number)
    m = re.match("(.*-[0-9]+)-.*", first_id)
    batch_id = m.group(1)

    return batch_id, sorted_ids, text_dict, label_dict


def generate_mp3_files(bucket, sorted_ids, text_dict, label_dict):

    # generate speech for each <4500 chars
    ssml = ""
    section_break = '<break time="{}s"/>'.format(SECTION_BREAK)
    caption_break = '<break time="{}s"/>'.format(CAPTION_BREAK)
    mp3_blob_list = []
    prev_id = None
    for id in sorted_ids:

        # split as chunks with <4500 chars each
        if len(ssml) + len(text_dict[id]) > 4500:
            mp3_blob = generate_mp3_for_ssml(bucket, prev_id, ssml)
            mp3_blob_list.append(mp3_blob)
            ssml = ""

        # add SSML tags based on the label
        if label_dict[id] == LABEL_BODY:
            ssml += "<p>" + text_dict[id] + "</p>\n"
        elif label_dict[id] == LABEL_CAPTION:
            ssml += caption_break + text_dict[id] + caption_break + "\n"
        elif label_dict[id] == LABEL_HEADER:
            ssml += section_break + text_dict[id] + section_break + "\n"

        prev_id = id

    # generate speech for the remaining
    mp3_blob = generate_mp3_for_ssml(bucket, prev_id, ssml)
    mp3_blob_list.append(mp3_blob)
    return mp3_blob_list


def generate_mp3_for_ssml(bucket, id, ssml):

    # set text and configs
    ssml = "<speak>\n" + ssml + "</speak>\n"
    synthesis_input = texttospeech.types.SynthesisInput(ssml=ssml)
    voice = texttospeech.types.VoiceSelectionParams(
        language_code="en-US", ssml_gender=texttospeech.enums.SsmlVoiceGender.MALE
    )
    audio_config = texttospeech.types.AudioConfig(
        audio_encoding=texttospeech.enums.AudioEncoding.MP3, speaking_rate=0.85
    )

    # generate speech
    try:
        response = speech_client.synthesize_speech(synthesis_input, voice, audio_config)
    except Exception as e:
        print("Retrying speech generation...")  # sometimes the api returns 500 error
        response = speech_client.synthesize_speech(synthesis_input, voice, audio_config)

    # save a MP3 file and delete the text file
    mp3_file_name = id + ".mp3"
    mp3_blob = bucket.blob(mp3_file_name)
    mp3_blob.upload_from_string(response.audio_content, content_type="audio/mpeg")
    print("MP3 file saved: {}".format(mp3_file_name))
    return mp3_blob


def merge_mp3_files(bucket, batch_id, mp3_blob_list):

    # merge saved mp3 files
    print("Started merging mp3 files for {}".format(batch_id))
    merged_mp3 = None
    for mp3_blob in mp3_blob_list:
        mp3_file = io.BytesIO(mp3_blob.download_as_string())
        mp3_data = AudioSegment.from_file(mp3_file, format="mp3")
        if merged_mp3:
            merged_mp3 += mp3_data
        else:
            merged_mp3 = mp3_data

    # save the merged mp3 file
    merged_mp3_file_name = (
        re.sub("[0-9][0-9]$", "", batch_id) + ".mp3"
    )  # 'foo-101' -> 'foo-1.mp3'
    merged_mp3_file_path = tempfile.gettempdir() + "/" + merged_mp3_file_name
    merged_mp3.export(merged_mp3_file_path, format="mp3")
    merged_mp3_blob = bucket.blob(merged_mp3_file_name)
    merged_mp3_blob.upload_from_filename(
        merged_mp3_file_path, content_type="audio/mpeg"
    )

    # delete mp3 files
    bucket.delete_blobs(mp3_blob_list)
    print("Ended merging mp3 files: {}".format(merged_mp3_file_name))
