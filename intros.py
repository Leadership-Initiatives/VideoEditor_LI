import os
import subprocess
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload, MediaInMemoryUpload
from google.oauth2.credentials import Credentials
import os
import boto3
from botocore.exceptions import NoCredentialsError
from retry import retry
import time
from googleapiclient.errors import ResumableUploadError
import ssl
import re
from moviepy.editor import VideoFileClip, AudioFileClip, CompositeVideoClip
import logging

def overlay_audio_and_upload(s3_url, output_filename, stitch_folder, service):
    # Download the video from S3
    response = requests.get(s3_url, stream=True)
    temp_video_path = 'temp_video.mp4'
    with open(temp_video_path, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)

    # Load the video and audio
    video = VideoFileClip(temp_video_path)
    audio = AudioFileClip("intro_audio.mp3")

    # If the audio is longer than the video, trim it
    if audio.duration > video.duration:
        audio = audio.subclip(0, video.duration)
    else:
        # If the audio is shorter, loop it
        audio = audio.audio_loop(duration=video.duration)

    # Overlay the audio
    final_clip = video.set_audio(audio)

    # Write the result to a file
    temp_output_path = 'temp_output.mp4'
    final_clip.write_videofile(temp_output_path, codec='libx264', audio_codec='aac')

    # Close the clips
    video.close()
    audio.close()
    final_clip.close()

    # Upload the file to Google Drive
    file_metadata = {'name': output_filename, 'parents': [stitch_folder]}
    media = MediaFileUpload(temp_output_path, resumable=True)
    file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
    try:
        # Clean up temporary files
        os.remove(temp_video_path)
        os.remove(temp_output_path)
    except:
        print("error removing temp files")
    return file['id']

def wait_for_s3_object(s3, bucket, key, local_filepath):
    """
    Wait for an S3 object to exist and have the same size as the local file.
    """
    local_filesize = os.path.getsize(local_filepath)
    while True:
        try:
            response = s3.head_object(Bucket=bucket, Key=key)
            if 'ContentLength' in response and response['ContentLength'] == local_filesize:
                return True
        except Exception as e:
            pass
        ("waited")
        time.sleep(1)

def create_shareable_link(service, file_id):
    try:
        # Create a permission for anyone with the link to view the file
        permission = {
            'type': 'anyone',
            'role': 'reader',
            'allowFileDiscovery': False
        }
        service.permissions().create(fileId=file_id, body=permission).execute()

        # Get the webViewLink (shareable link) for the file
        file = service.files().get(fileId=file_id, fields='webViewLink').execute()
        return file.get('webViewLink')
    except Exception as e:
        print(f"Error creating shareable link: {e}")
        return None
    



       
def concatenate_videos_aws(intro_resized_filename, main_filename, output_filename, service, stitch_folder):
    # Dictionary to hold AWS credentials
    aws_credentials = {}

    # Read AWS details directly from amazon.txt
    with open("amazon.txt", 'r') as file:
        for line in file:
            # Clean up line to remove potential hidden characters like '\r'
            line = line.strip().replace('\r', '')
            if ' = ' in line:
                key, value = line.split(' = ')
                aws_credentials[key] = value.strip("'")

    # Assign variables from the dictionary if they exist
    AWS_REGION_NAME = aws_credentials.get('AWS_REGION_NAME', 'Default-Region')
    AWS_ACCESS_KEY = aws_credentials.get('AWS_ACCESS_KEY', 'Default-Access-Key')
    AWS_SECRET_KEY = aws_credentials.get('AWS_SECRET_KEY', 'Default-Secret-Key')

    BUCKET_NAME = 'li-general-task'
    S3_INPUT_PREFIX = 'input_videos/'
    S3_OUTPUT_PREFIX = 'output_videos/'

    # Initialize the S3 client
    s3 = boto3.client('s3',
        region_name=AWS_REGION_NAME,
        aws_access_key_id=AWS_ACCESS_KEY,
        aws_secret_access_key=AWS_SECRET_KEY
    )

    # Create a unique identifier based on the main file name
    unique_id = main_filename.rsplit('_', 1)[0]


    # Initialize boto3 client for AWS MediaConvert
    client = boto3.client('mediaconvert',
        region_name=AWS_REGION_NAME,
        aws_access_key_id=AWS_ACCESS_KEY,
        aws_secret_access_key=AWS_SECRET_KEY,
        endpoint_url='https://wa11sy9gb.mediaconvert.us-east-2.amazonaws.com'
    )

    def wait_for_job_completion(client, job_id):
        while True:
            response = client.get_job(Id=job_id)
            status = response['Job']['Status']
            if status in ['COMPLETE', 'ERROR', 'CANCELED']:
                return status
            time.sleep(5)

    # Define the input files and their roles
    inputs = [
        {
            'FileInput': f's3://{BUCKET_NAME}/{S3_INPUT_PREFIX}{intro_resized_filename}',
            'AudioSelectors': {
                'Audio Selector 1': {
                    'DefaultSelection': 'DEFAULT',
                    'SelectorType': 'TRACK',
                    'Offset': 0,
                    'ProgramSelection': 1,
                }
            }
        },
        {
            'FileInput': f's3://{BUCKET_NAME}/{S3_INPUT_PREFIX}{main_filename}',
            'AudioSelectors': {
                'Audio Selector 2': {
                    'DefaultSelection': 'DEFAULT',
                    'SelectorType': 'TRACK',
                    'Offset': 0,
                    'ProgramSelection': 1,
                }
            }
        }
    ]

    output = {
        'Extension': 'mp4',
        'ContainerSettings': {
            'Container': 'MP4',
            'Mp4Settings': {
                'CslgAtom': 'INCLUDE',
                'FreeSpaceBox': 'EXCLUDE',
                'MoovPlacement': 'PROGRESSIVE_DOWNLOAD'
            }
        },
        'VideoDescription': {
            'CodecSettings': {
                'Codec': 'H_264',
                'H264Settings': {
                    'CodecProfile': 'MAIN',
                    'CodecLevel': 'AUTO',
                    'RateControlMode': 'QVBR',
                    'MaxBitrate': 5000000  # Example value, adjust as needed
                }
            }
        },
        'AudioDescriptions': [{
            'AudioSourceName': 'Audio Selector 1', # Specify the name of the audio selector
            'CodecSettings': {
                'Codec': 'AAC',
                'AacSettings': {
                    'AudioDescriptionBroadcasterMix': 'NORMAL',
                    'RateControlMode': 'CBR',
                    'CodecProfile': 'LC',
                    'CodingMode': 'CODING_MODE_2_0',
                    'SampleRate': 48000,
                    'Bitrate': 96000
                }
            }
        }]
    }

    # Create the job settings
    job_settings = {
        'Inputs': inputs,
        'OutputGroups': [{
            'Name': 'File Group',
            'Outputs': [output],
            'OutputGroupSettings': {
                'Type': 'FILE_GROUP_SETTINGS',
                'FileGroupSettings': {
                    'Destination': f's3://{BUCKET_NAME}/{S3_OUTPUT_PREFIX}{output_filename.rsplit(".", 1)[0]}'
                }
            }
        }]
    }


    try:
        max_retries = 3
        retry_count = 0

        while retry_count < max_retries:
            try:
                # Submit the job
                response = client.create_job(Role='arn:aws:iam::339713096623:role/MediaConvertRole', Settings=job_settings)
                job_id = response['Job']['Id']
                print('Job created:', job_id)

                # Wait for the job to finish
                job_status = wait_for_job_completion(client, job_id)
                if job_status == 'COMPLETE':
                    print('Job completed successfully.')
                    break
                elif job_status == 'ERROR':
                    response = client.get_job(Id=job_id)
                    error_message = response['Job'].get('ErrorMessage', 'No error message provided.')
                    print('Job failed with error:', error_message)
                    retry_count += 1
                    print(f"Retrying job submission... (Attempt {retry_count}/{max_retries})")
                else:
                    print(f'Job failed with status: {job_status}')
                    return
            except Exception as e:
                print('Error:', e)
                retry_count += 1
                print(f"Retrying job submission... (Attempt {retry_count}/{max_retries})")

        if retry_count == max_retries:
            print('Maximum number of retries reached. Job submission failed.')
            return
    
        # Use the new function to overlay audio and upload to Google Drive
        s3_url = f'https://{BUCKET_NAME}.s3.amazonaws.com/{S3_OUTPUT_PREFIX}{output_filename.rsplit(".", 1)[0]}.mp4'
        file_id = overlay_audio_and_upload(s3_url, output_filename, stitch_folder, service)
        print(f"File uploaded to Google Drive with ID: {file_id}")

        try:
            # Clean up the S3 bucket by deleting the files
            s3.delete_object(Bucket=BUCKET_NAME, Key=S3_INPUT_PREFIX + f'{unique_id}_intro.mp4')
            s3.delete_object(Bucket=BUCKET_NAME, Key=S3_INPUT_PREFIX + f'{unique_id}_main.mp4')
            s3.delete_object(Bucket=BUCKET_NAME, Key=S3_OUTPUT_PREFIX + output_filename.rsplit(".", 1)[0] + '.mp4')
        except:
            return file_id
        return file_id

    except Exception as e:
        print('Error:', e)
        return file_id

    return

@retry((ssl.SSLEOFError, ResumableUploadError), tries=5, delay=2, backoff=2)
def download_video(file_id, filename, service):
    try:
        request = service.files().get_media(fileId=file_id)
        with open(filename, 'wb') as f:
            downloader = MediaIoBaseDownload(f, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()
    except:
        print("retried!")
    
def upload_video(stream, folder_id, service, output_filename):
    file_metadata = {'name': output_filename, 'parents': [folder_id]}
    media = MediaInMemoryUpload(stream.read(), mimetype='video/mp4', resumable=True)
    try:
        return service.files().create(body=file_metadata, media_body=media, fields='id').execute()
    except (ssl.SSLEOFError, ResumableUploadError) as e:
        print(f"{e.__class__.__name__} encountered, retrying...")
        raise

import requests
from io import BytesIO

def intro_process_video(data):
    row_number, row, videos_directory, creds_dict, stitch_folder, sheet_id = data
    creds = Credentials.from_authorized_user_info(creds_dict)
    service = build('drive', 'v3', credentials=creds)
    sheets_service = build('sheets', 'v4', credentials=creds)
    # Create a unique identifier based on the row name
    unique_id = row['name']

    # Dictionary to hold AWS credentials
    aws_credentials = {}

    # Read AWS details directly from amazon.txt
    with open("amazon.txt", 'r') as file:
        for line in file:
            # Clean up line to remove potential hidden characters like '\r'
            line = line.strip().replace('\r', '')
            if ' = ' in line:
                key, value = line.split(' = ')
                aws_credentials[key] = value.strip("'")

    # Assign variables from the dictionary if they exist
    AWS_REGION_NAME = aws_credentials.get('AWS_REGION_NAME', 'Default-Region')
    AWS_ACCESS_KEY = aws_credentials.get('AWS_ACCESS_KEY', 'Default-Access-Key')
    AWS_SECRET_KEY = aws_credentials.get('AWS_SECRET_KEY', 'Default-Secret-Key')

    BUCKET_NAME = 'li-general-task'
    S3_INPUT_PREFIX = 'input_videos/'
    S3_OUTPUT_PREFIX = 'output_videos/'

    # Initialize the S3 client
    s3 = boto3.client('s3',
        region_name=AWS_REGION_NAME,
        aws_access_key_id=AWS_ACCESS_KEY,
        aws_secret_access_key=AWS_SECRET_KEY
    )

    # Check if the main video is a local file or a Google Drive URL
    if row['main'].startswith('http'):
        # It's a Google Drive URL
        main_file_id = row['main'].split("/file/d/")[1].split("/view")[0]
        stream_video_to_s3(service, main_file_id, f'{unique_id}_main.mp4', s3, BUCKET_NAME, S3_INPUT_PREFIX)
    else:
        # It's a local file
        s3.upload_file(row['main'], BUCKET_NAME, S3_INPUT_PREFIX + f'{unique_id}_main.mp4')

    # Check if the intro video is a local file or a Google Drive URL
    if row['intro'].startswith('http'):
        # It's a Google Drive URL
        intro_file_id = row['intro'].split("/file/d/")[1].split("/view")[0]
        stream_video_to_s3(service, intro_file_id, f'{unique_id}_intro.mp4', s3, BUCKET_NAME, S3_INPUT_PREFIX)
    else:
        # It's a local file
        s3.upload_file(row['intro'], BUCKET_NAME, S3_INPUT_PREFIX + f'{unique_id}_intro.mp4')

    # Concatenate video clips
    output_filename = f"{row['name']}"

    # After concatenating videos and uploading to Google Drive
    file_id = concatenate_videos_aws(f'{unique_id}_intro.mp4', f'{unique_id}_main.mp4', output_filename, service, stitch_folder)
    print(file_id)
    # Generate the share link
    try:
        file = service.files().get(fileId=file_id, fields='webViewLink').execute()
        share_link = file.get('webViewLink')

        # Update the Google Sheet with the share link
        range_name = f'A{row_number+2}:Z{row_number+2}'  # Assuming row_number is 0-indexed
        result = sheets_service.spreadsheets().values().get(spreadsheetId=sheet_id, range=range_name).execute()
        row_values = result.get('values', [[]])[0]
        
        # Find the 'link' column or add it if it doesn't exist
        headers = sheets_service.spreadsheets().values().get(spreadsheetId=sheet_id, range='A1:Z1').execute().get('values', [[]])[0]
        if 'link' not in headers:
            headers.append('link')
            sheets_service.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range='A1:Z1',
                valueInputOption='RAW',
                body={'values': [headers]}
            ).execute()
        
        link_column = headers.index('link')
        
        # Extend row_values if necessary
        while len(row_values) <= link_column:
            row_values.append('')
        

        
        row_values[link_column] = share_link
        logging.info(f"Updated row values: {row_values}")

        update_result = sheets_service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=range_name,
            valueInputOption='RAW',
            body={'values': [row_values]}
        ).execute()
        print(f"Sheet update result: {update_result}")

    except HttpError as error:
        print(f'An error occurred: {error}')

    return row['name']

def stream_video_to_s3(service, file_id, s3_filename, s3_client, bucket_name, s3_prefix):
    request = service.files().get_media(fileId=file_id)
    response = request.execute()
    stream = BytesIO(response)
    s3_client.upload_fileobj(stream, bucket_name, s3_prefix + s3_filename)